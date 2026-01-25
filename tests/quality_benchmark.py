#!/usr/bin/env python3
"""
Quality Benchmark Framework
===========================
Compares AI output quality against human-defined gold standards.

This framework measures:
1. Field Accuracy - Are extracted values correct?
2. Notes Quality - Is context captured without redundancy?
3. Response Quality - Is the email professional and appropriate?
4. Event Detection - Are escalations caught correctly?

Usage:
    python tests/quality_benchmark.py                    # Run all benchmarks
    python tests/quality_benchmark.py --verbose          # Detailed output
    python tests/quality_benchmark.py --report           # Generate HTML report
"""

import os
import sys
import json
import argparse
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import re

# Set test env vars before any imports
if not os.getenv("AZURE_API_APP_ID"):
    os.environ["AZURE_API_APP_ID"] = "test-app-id"
if not os.getenv("AZURE_API_CLIENT_SECRET"):
    os.environ["AZURE_API_CLIENT_SECRET"] = "test-secret"
if not os.getenv("FIREBASE_API_KEY"):
    os.environ["FIREBASE_API_KEY"] = "test-firebase-key"

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firebase/Firestore before importing production code
from unittest.mock import MagicMock
import sys as _sys

mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
_sys.modules['google.cloud.firestore'] = mock_firestore
_sys.modules['google.cloud'] = MagicMock()
_sys.modules['google.oauth2.credentials'] = MagicMock()
_sys.modules['google.auth.transport.requests'] = MagicMock()
_sys.modules['googleapiclient.discovery'] = MagicMock()

from email_automation.ai_processing import propose_sheet_updates


@dataclass
class QualityScore:
    """Quality scores for a single test."""
    field_accuracy: float = 0.0      # 0-1: correct values / expected values
    field_completeness: float = 0.0  # 0-1: extracted fields / available fields
    notes_quality: float = 0.0       # 0-1: contextual info captured, no redundancy
    response_quality: float = 0.0    # 0-1: professional, appropriate, concise
    event_accuracy: float = 0.0      # 0-1: correct events detected
    overall: float = 0.0             # Weighted average

    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkCase:
    """A benchmark test case with gold standard expected output."""
    name: str
    description: str

    # Input
    property_data: Dict[str, str]
    conversation: List[Dict[str, str]]

    # Gold standard (what a human would produce)
    expected_updates: List[Dict[str, Any]]
    expected_notes: str  # What notes SHOULD contain
    forbidden_in_notes: List[str]  # What notes should NOT contain (redundant data)
    expected_events: List[str]
    expected_response_type: str  # "closing", "request_info", "escalate", "none"
    response_should_mention: List[str]  # Key points email should reference

    # Quality weights (customizable per test)
    weights: Dict[str, float] = field(default_factory=lambda: {
        "field_accuracy": 0.3,
        "field_completeness": 0.2,
        "notes_quality": 0.2,
        "response_quality": 0.2,
        "event_accuracy": 0.1,
    })


# ============================================================
# BENCHMARK CASES - Gold Standard Expected Outputs
# ============================================================

BENCHMARK_CASES = [
    BenchmarkCase(
        name="complete_industrial_property",
        description="Broker provides all specs for industrial warehouse",
        property_data={
            "Property Address": "100 Industrial Way",
            "City": "Augusta",
            "Leasing Company": "Augusta Commercial",
            "Leasing Contact": "Mike Johnson",
            "Email": "mike@augustacommercial.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Mike, I'm interested in 100 Industrial Way. Could you provide the property details?"},
            {"direction": "inbound", "content": """Jill,

Happy to help! Here's what we have for 100 Industrial Way:

- 25,000 SF warehouse
- Asking $7.50/SF NNN
- CAM/OpEx: $1.85/SF
- 2 drive-in doors, 4 dock doors
- 28' clear height
- 400 amps, 3-phase power

The building was renovated in 2022, has a fully fenced yard with 12 trailer spots, and the owner is motivated - flexible on 3-5 year terms. Located right off I-520.

Let me know if you'd like to schedule a tour!

Mike"""},
        ],
        expected_updates=[
            {"column": "Total SF", "value": "25000"},
            {"column": "Rent/SF /Yr", "value": "7.50"},
            {"column": "Ops Ex /SF", "value": "1.85"},
            {"column": "Drive Ins", "value": "2"},
            {"column": "Docks", "value": "4"},
            {"column": "Ceiling Ht", "value": "28"},
            {"column": "Power", "value": "400 amps, 3-phase"},
        ],
        expected_notes="NNN • renovated 2022 • fenced yard • 12 trailer spots • owner motivated • flexible 3-5 yr • near I-520",
        forbidden_in_notes=["25000", "25,000", "7.50", "1.85", "28", "400"],  # Don't repeat column values
        expected_events=[],
        expected_response_type="closing",
        response_should_mention=["thank", "details", "tour"],
    ),

    BenchmarkCase(
        name="partial_info_needs_followup",
        description="Broker provides some info, need to request missing fields",
        property_data={
            "Property Address": "200 Commerce Dr",
            "City": "North Augusta",
            "Leasing Company": "NAC Realty",
            "Leasing Contact": "Sarah Chen",
            "Email": "sarah@nacrealty.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Sarah, Reaching out about 200 Commerce Dr. Can you send over the property specs?"},
            {"direction": "inbound", "content": """Hi Jill,

200 Commerce Dr is 18,000 SF, asking $6.25/SF triple net.

Sarah"""},
        ],
        expected_updates=[
            {"column": "Total SF", "value": "18000"},
            {"column": "Rent/SF /Yr", "value": "6.25"},
        ],
        expected_notes="NNN",  # Minimal - just lease type
        forbidden_in_notes=["18000", "18,000", "6.25"],
        expected_events=[],
        expected_response_type="request_info",
        response_should_mention=["ops ex", "drive", "dock", "ceiling", "power"],  # Should ask for missing
    ),

    BenchmarkCase(
        name="property_unavailable_with_alternative",
        description="Broker says property unavailable but suggests another",
        property_data={
            "Property Address": "300 Warehouse Blvd",
            "City": "Evans",
            "Leasing Company": "Evans Industrial",
            "Leasing Contact": "Tom Baker",
            "Email": "tom@evansind.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Tom, Writing about 300 Warehouse Blvd. Is it still available?"},
            {"direction": "inbound", "content": """Jill,

Unfortunately 300 Warehouse Blvd just went under contract last week.

However, I have a similar property at 350 Warehouse Blvd that might work -
it's 22,000 SF, $6.75/SF NNN. Contact Lisa Park at lisa@evansind.com for details.

Tom"""},
        ],
        expected_updates=[],
        expected_notes="",
        forbidden_in_notes=[],
        expected_events=["property_unavailable", "new_property"],
        expected_response_type="none",  # Escalate to user, no auto-response
        response_should_mention=[],
    ),

    BenchmarkCase(
        name="negotiation_counteroffer",
        description="Broker makes counteroffer requiring user decision",
        property_data={
            "Property Address": "400 Distribution Center",
            "City": "Augusta",
            "Leasing Company": "Prime Industrial",
            "Leasing Contact": "Dave Wilson",
            "Email": "dave@primeindustrial.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Dave, Following up on 400 Distribution Center. The asking rent seems high - any flexibility?"},
            {"direction": "inbound", "content": """Jill,

I talked to the owner. Best we can do is $8.25/SF if your client signs a 5-year lease instead of 3.

Would that work?

Dave"""},
        ],
        expected_updates=[],
        expected_notes="counteroffer: $8.25/SF for 5-year term",
        forbidden_in_notes=[],
        expected_events=["needs_user_input"],
        expected_response_type="none",  # Escalate negotiation to user
        response_should_mention=[],
    ),

    BenchmarkCase(
        name="call_requested_with_phone",
        description="Broker wants to discuss by phone",
        property_data={
            "Property Address": "500 Tech Park",
            "City": "Martinez",
            "Leasing Company": "Martinez Properties",
            "Leasing Contact": "Jennifer Lee",
            "Email": "jennifer@martinezprop.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Jennifer, Could you provide details on 500 Tech Park?"},
            {"direction": "inbound", "content": """Hi Jill,

There's a lot to discuss about this one - complex TI situation and the owner has some specific requirements.

Can you call me at 706-555-1234? Easier to explain by phone.

Jennifer"""},
        ],
        expected_updates=[],
        expected_notes="complex TI situation • owner has specific requirements • prefers phone discussion",
        forbidden_in_notes=[],
        expected_events=["call_requested"],
        expected_response_type="none",
        response_should_mention=[],
    ),

    BenchmarkCase(
        name="tour_offered",
        description="Broker offers to schedule a tour",
        property_data={
            "Property Address": "600 Business Park",
            "City": "Augusta",
            "Leasing Company": "Augusta Brokers",
            "Leasing Contact": "Chris Martinez",
            "Email": "chris@augustabrokers.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Chris, What can you tell me about 600 Business Park?"},
            {"direction": "inbound", "content": """Jill,

600 Business Park is a great space - 30,000 SF, $7.00/SF NNN, op ex around $2.10/SF.
Recently renovated with new LED lighting throughout.

I'm available Thursday or Friday to show it. Would either work for you?

Chris"""},
        ],
        expected_updates=[
            {"column": "Total SF", "value": "30000"},
            {"column": "Rent/SF /Yr", "value": "7.00"},
            {"column": "Ops Ex /SF", "value": "2.10"},
        ],
        expected_notes="NNN • recently renovated • new LED lighting",
        forbidden_in_notes=["30000", "30,000", "7.00", "2.10"],
        expected_events=["tour_requested"],
        expected_response_type="none",  # Let user decide on tour
        response_should_mention=[],
    ),

    BenchmarkCase(
        name="identity_question_escalate",
        description="Broker asks about client identity - must escalate",
        property_data={
            "Property Address": "700 Industrial Blvd",
            "City": "Evans",
            "Leasing Company": "Evans Realty",
            "Leasing Contact": "Mark Thompson",
            "Email": "mark@evansrealty.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Mark, Interested in 700 Industrial Blvd for a client. What are the specs?"},
            {"direction": "inbound", "content": """Jill,

Before I send specs, who's your client? I like to know who I'm dealing with.

Mark"""},
        ],
        expected_updates=[],
        expected_notes="",
        forbidden_in_notes=[],
        expected_events=["needs_user_input"],
        expected_response_type="none",
        response_should_mention=[],
    ),

    BenchmarkCase(
        name="contact_opted_out",
        description="Broker says don't contact them",
        property_data={
            "Property Address": "800 Commerce Way",
            "City": "Augusta",
            "Leasing Company": "Augusta Commercial",
            "Leasing Contact": "Robert Kim",
            "Email": "robert@augustacomm.com",
        },
        conversation=[
            {"direction": "outbound", "content": "Hi Robert, Reaching out about 800 Commerce Way."},
            {"direction": "inbound", "content": """Please remove me from your list. We don't work with tenant reps.

Robert"""},
        ],
        expected_updates=[],
        expected_notes="",
        forbidden_in_notes=[],
        expected_events=["contact_optout"],
        expected_response_type="none",
        response_should_mention=[],
    ),
]


def score_field_accuracy(actual_updates: List[Dict], expected_updates: List[Dict]) -> tuple:
    """Score how accurately fields were extracted."""
    if not expected_updates:
        return 1.0, {"no_expected": True}

    correct = 0
    details = {"correct": [], "incorrect": [], "missing": []}

    actual_by_col = {u["column"]: u["value"] for u in actual_updates}

    for expected in expected_updates:
        col = expected["column"]
        exp_val = str(expected["value"])

        if col in actual_by_col:
            actual_val = str(actual_by_col[col])
            # Normalize for comparison (remove commas, trim)
            exp_norm = exp_val.replace(",", "").strip()
            act_norm = actual_val.replace(",", "").strip()

            if exp_norm == act_norm:
                correct += 1
                details["correct"].append(col)
            else:
                details["incorrect"].append({"column": col, "expected": exp_val, "actual": actual_val})
        else:
            details["missing"].append(col)

    score = correct / len(expected_updates) if expected_updates else 1.0
    return score, details


def score_field_completeness(actual_updates: List[Dict], expected_updates: List[Dict]) -> tuple:
    """Score how many available fields were captured."""
    if not expected_updates:
        return 1.0, {"no_expected": True}

    actual_cols = {u["column"] for u in actual_updates}
    expected_cols = {u["column"] for u in expected_updates}

    captured = len(actual_cols & expected_cols)
    total = len(expected_cols)

    score = captured / total if total > 0 else 1.0
    return score, {"captured": captured, "total": total}


def score_notes_quality(actual_notes: str, expected_notes: str, forbidden: List[str]) -> tuple:
    """Score notes quality - contextual info without redundancy."""
    details = {"redundant": [], "captured": [], "missing": []}

    # Check for forbidden redundant content
    redundancy_penalty = 0
    for forbidden_item in forbidden:
        if forbidden_item.lower() in actual_notes.lower():
            redundancy_penalty += 0.15
            details["redundant"].append(forbidden_item)

    # Check for expected contextual content
    expected_parts = [p.strip() for p in expected_notes.split("•") if p.strip()]
    if expected_parts:
        captured = 0
        for part in expected_parts:
            # Fuzzy match - check if key words appear
            key_words = [w for w in part.lower().split() if len(w) > 3]
            if any(kw in actual_notes.lower() for kw in key_words):
                captured += 1
                details["captured"].append(part)
            else:
                details["missing"].append(part)

        context_score = captured / len(expected_parts) if expected_parts else 1.0
    else:
        # No expected notes - just check it's not redundant
        context_score = 1.0 if not actual_notes else 0.8

    score = max(0, context_score - redundancy_penalty)
    return score, details


def score_response_quality(response: str, expected_type: str, should_mention: List[str]) -> tuple:
    """Score response email quality."""
    details = {"type_match": False, "mentions_found": [], "mentions_missing": []}

    has_response = response and response.strip()

    # Check response type
    if expected_type == "none":
        # Should NOT have a response
        if not has_response:
            details["type_match"] = True
            return 1.0, details
        else:
            return 0.3, details  # Penalty for responding when shouldn't

    if not has_response:
        return 0.0, {"error": "Expected response but got none"}

    # Check response type
    response_lower = response.lower()
    if expected_type == "closing":
        details["type_match"] = "thank" in response_lower
    elif expected_type == "request_info":
        details["type_match"] = any(w in response_lower for w in ["could you", "please", "confirm", "provide"])
    else:
        details["type_match"] = True

    type_score = 1.0 if details["type_match"] else 0.5

    # Check for required mentions
    mention_score = 1.0
    if should_mention:
        found = 0
        for mention in should_mention:
            if mention.lower() in response_lower:
                found += 1
                details["mentions_found"].append(mention)
            else:
                details["mentions_missing"].append(mention)
        mention_score = found / len(should_mention)

    score = (type_score * 0.6) + (mention_score * 0.4)
    return score, details


def score_event_accuracy(actual_events: List[str], expected_events: List[str]) -> tuple:
    """Score event detection accuracy."""
    if not expected_events and not actual_events:
        return 1.0, {"no_events_expected": True}

    actual_set = set(actual_events)
    expected_set = set(expected_events)

    correct = actual_set & expected_set
    missed = expected_set - actual_set
    extra = actual_set - expected_set

    # Score: correct matches minus penalties
    if expected_set:
        base_score = len(correct) / len(expected_set)
    else:
        base_score = 0.0 if extra else 1.0

    # Penalty for extra events (false positives)
    penalty = len(extra) * 0.2

    score = max(0, base_score - penalty)
    return score, {"correct": list(correct), "missed": list(missed), "extra": list(extra)}


# Standard sheet header
HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments", "Flyer / Link", "Floorplan", "Jill and Clients comments"
]


def run_benchmark(case: BenchmarkCase, verbose: bool = False) -> QualityScore:
    """Run a single benchmark and return quality scores."""
    # Build conversation in the format expected by propose_sheet_updates
    conversation = []
    for msg in case.conversation:
        conversation.append({
            "direction": msg["direction"],
            "body": msg["content"],
            "from": case.property_data.get("Email", "broker@test.com") if msg["direction"] == "inbound" else "jill@mohrpartners.com",
            "to": ["jill@mohrpartners.com"] if msg["direction"] == "inbound" else [case.property_data.get("Email", "broker@test.com")],
            "subject": f"{case.property_data['Property Address']}, {case.property_data.get('City', 'Augusta')}",
            "timestamp": "2026-01-25T12:00:00Z",
        })

    # Build row values matching header
    rowvals = [""] * len(HEADER)
    for key, val in case.property_data.items():
        if key in HEADER:
            rowvals[HEADER.index(key)] = val

    # Call production AI with correct signature
    result = propose_sheet_updates(
        uid="benchmark-user",
        client_id="benchmark-client",
        email=case.property_data.get("Email", "broker@test.com"),
        sheet_id="benchmark-sheet-id",
        header=HEADER,
        rownum=2,
        rowvals=rowvals,
        thread_id=f"benchmark-thread-{case.name}",
        contact_name=case.property_data.get("Leasing Contact", "Broker"),
        conversation=conversation,
        dry_run=True,
    )

    if not result:
        return QualityScore(details={"error": "AI returned None"})

    # Extract results
    actual_updates = result.get("updates", [])
    actual_events = [e.get("type") for e in result.get("events", [])]
    actual_notes = result.get("notes", "")
    actual_response = result.get("response_email", "")

    # Score each dimension
    field_acc, field_acc_details = score_field_accuracy(actual_updates, case.expected_updates)
    field_comp, field_comp_details = score_field_completeness(actual_updates, case.expected_updates)
    notes_qual, notes_details = score_notes_quality(actual_notes, case.expected_notes, case.forbidden_in_notes)
    resp_qual, resp_details = score_response_quality(actual_response, case.expected_response_type, case.response_should_mention)
    event_acc, event_details = score_event_accuracy(actual_events, case.expected_events)

    # Calculate weighted overall
    weights = case.weights
    overall = (
        field_acc * weights["field_accuracy"] +
        field_comp * weights["field_completeness"] +
        notes_qual * weights["notes_quality"] +
        resp_qual * weights["response_quality"] +
        event_acc * weights["event_accuracy"]
    )

    score = QualityScore(
        field_accuracy=field_acc,
        field_completeness=field_comp,
        notes_quality=notes_qual,
        response_quality=resp_qual,
        event_accuracy=event_acc,
        overall=overall,
        details={
            "field_accuracy": field_acc_details,
            "field_completeness": field_comp_details,
            "notes_quality": notes_details,
            "response_quality": resp_details,
            "event_accuracy": event_details,
            "actual": {
                "updates": actual_updates,
                "events": actual_events,
                "notes": actual_notes,
                "response": actual_response[:200] if actual_response else None,
            },
        }
    )

    if verbose:
        print(f"\n{'='*60}")
        print(f"Benchmark: {case.name}")
        print(f"{'='*60}")
        print(f"Field Accuracy:    {field_acc:.2f}")
        print(f"Field Completeness:{field_comp:.2f}")
        print(f"Notes Quality:     {notes_qual:.2f}")
        print(f"Response Quality:  {resp_qual:.2f}")
        print(f"Event Accuracy:    {event_acc:.2f}")
        print(f"OVERALL:           {overall:.2f}")
        if notes_details.get("redundant"):
            print(f"  ⚠️ Redundant in notes: {notes_details['redundant']}")
        if actual_notes:
            print(f"  Notes: {actual_notes}")

    return score


def run_all_benchmarks(verbose: bool = False) -> Dict[str, Any]:
    """Run all benchmarks and return summary."""
    results = {}
    totals = {
        "field_accuracy": 0,
        "field_completeness": 0,
        "notes_quality": 0,
        "response_quality": 0,
        "event_accuracy": 0,
        "overall": 0,
    }

    print("\n" + "="*70)
    print("QUALITY BENCHMARK SUITE")
    print("="*70)
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Cases: {len(BENCHMARK_CASES)}")
    print()

    for case in BENCHMARK_CASES:
        print(f"  Running: {case.name}...", end=" ", flush=True)
        try:
            score = run_benchmark(case, verbose=verbose)
            results[case.name] = score

            for key in totals:
                totals[key] += getattr(score, key)

            emoji = "✅" if score.overall >= 0.8 else "⚠️" if score.overall >= 0.6 else "❌"
            print(f"{emoji} {score.overall:.2f}")

        except Exception as e:
            print(f"❌ Error: {e}")
            results[case.name] = QualityScore(details={"error": str(e)})

    # Calculate averages
    n = len(BENCHMARK_CASES)
    averages = {k: v/n for k, v in totals.items()}

    # Summary
    print("\n" + "="*70)
    print("QUALITY SUMMARY")
    print("="*70)
    print(f"  Field Accuracy:     {averages['field_accuracy']:.2%}")
    print(f"  Field Completeness: {averages['field_completeness']:.2%}")
    print(f"  Notes Quality:      {averages['notes_quality']:.2%}")
    print(f"  Response Quality:   {averages['response_quality']:.2%}")
    print(f"  Event Accuracy:     {averages['event_accuracy']:.2%}")
    print(f"  ─────────────────────────────")
    print(f"  OVERALL QUALITY:    {averages['overall']:.2%}")

    # Quality grade
    overall = averages['overall']
    if overall >= 0.95:
        grade = "A+ (Excellent)"
    elif overall >= 0.90:
        grade = "A (Very Good)"
    elif overall >= 0.85:
        grade = "B+ (Good)"
    elif overall >= 0.80:
        grade = "B (Acceptable)"
    elif overall >= 0.70:
        grade = "C (Needs Improvement)"
    else:
        grade = "D (Poor)"

    print(f"\n  QUALITY GRADE: {grade}")

    return {
        "results": {k: {"overall": v.overall, "details": v.details} for k, v in results.items()},
        "averages": averages,
        "grade": grade,
        "timestamp": datetime.now().isoformat(),
    }


def generate_html_report(data: Dict, output_path: str):
    """Generate an HTML quality report."""
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Quality Benchmark Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; }}
        h1 {{ color: #333; }}
        .summary {{ background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0; }}
        .grade {{ font-size: 24px; font-weight: bold; color: #2e7d32; }}
        .metric {{ display: inline-block; margin: 10px 20px 10px 0; }}
        .metric-label {{ font-size: 12px; color: #666; }}
        .metric-value {{ font-size: 20px; font-weight: bold; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background: #f9f9f9; }}
        .pass {{ color: #2e7d32; }}
        .warn {{ color: #f57c00; }}
        .fail {{ color: #c62828; }}
    </style>
</head>
<body>
    <h1>Quality Benchmark Report</h1>
    <p>Generated: {data['timestamp']}</p>

    <div class="summary">
        <div class="grade">{data['grade']}</div>
        <div style="margin-top: 15px;">
            <div class="metric">
                <div class="metric-label">Field Accuracy</div>
                <div class="metric-value">{data['averages']['field_accuracy']:.1%}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Completeness</div>
                <div class="metric-value">{data['averages']['field_completeness']:.1%}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Notes Quality</div>
                <div class="metric-value">{data['averages']['notes_quality']:.1%}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Response Quality</div>
                <div class="metric-value">{data['averages']['response_quality']:.1%}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Event Accuracy</div>
                <div class="metric-value">{data['averages']['event_accuracy']:.1%}</div>
            </div>
        </div>
    </div>

    <h2>Individual Results</h2>
    <table>
        <tr>
            <th>Benchmark</th>
            <th>Overall</th>
            <th>Fields</th>
            <th>Notes</th>
            <th>Response</th>
            <th>Events</th>
        </tr>
"""

    for name, result in data['results'].items():
        overall = result['overall']
        css_class = "pass" if overall >= 0.8 else "warn" if overall >= 0.6 else "fail"

        # Get individual scores from details if available
        details = result.get('details', {})

        html += f"""        <tr>
            <td>{name}</td>
            <td class="{css_class}">{overall:.1%}</td>
            <td>-</td>
            <td>-</td>
            <td>-</td>
            <td>-</td>
        </tr>
"""

    html += """    </table>
</body>
</html>"""

    with open(output_path, 'w') as f:
        f.write(html)
    print(f"\n📊 HTML report saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Quality Benchmark Framework")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detailed output")
    parser.add_argument("--report", action="store_true", help="Generate HTML report")
    parser.add_argument("--output", default="tests/results/quality_benchmark.html", help="Report output path")
    args = parser.parse_args()

    data = run_all_benchmarks(verbose=args.verbose)

    if args.report:
        generate_html_report(data, args.output)

    # Save JSON results
    json_path = args.output.replace('.html', '.json')
    with open(json_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"📄 JSON results saved to: {json_path}")


if __name__ == "__main__":
    main()
