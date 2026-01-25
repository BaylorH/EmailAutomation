#!/usr/bin/env python3
"""
Test Results Analyzer
=====================
Analyzes batch test results to identify patterns, issues, and insights.

Usage:
    python tests/analyze_results.py --results tests/results/batch_50/
    python tests/analyze_results.py --results tests/results/batch_50/ --compare tests/results/batch_100/
    python tests/analyze_results.py --results tests/results/batch_50/ --export-html report.html
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class AnalysisResult:
    """Analysis result for a test run."""
    run_id: str
    total_tests: int
    passed: int
    failed: int
    pass_rate: float

    # Performance
    avg_latency_ms: float
    p50_latency_ms: float
    p90_latency_ms: float
    p99_latency_ms: float

    # By category
    by_category: Dict[str, Dict]

    # Issue patterns
    common_issues: List[Dict]

    # Field extraction stats
    field_stats: Dict[str, Dict]

    # Event stats
    event_stats: Dict[str, int]

    # Response email stats
    response_stats: Dict[str, int]


# ============================================================================
# ANALYSIS FUNCTIONS
# ============================================================================

def load_results(results_path: Path) -> Tuple[Dict, List[Dict]]:
    """Load summary and all results from a results directory."""
    summary_path = results_path / "summary.json"
    all_results_path = results_path / "all_results.json"

    summary = {}
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)

    all_results = []
    if all_results_path.exists():
        with open(all_results_path) as f:
            all_results = json.load(f)

    return summary, all_results


def analyze_field_extraction(results: List[Dict]) -> Dict[str, Dict]:
    """Analyze which fields are being extracted and their accuracy."""
    field_stats = defaultdict(lambda: {
        "count": 0,
        "avg_confidence": 0,
        "values": [],
        "issues": []
    })

    for r in results:
        for update in r.get("updates", []):
            col = update.get("column", "unknown")
            confidence = update.get("confidence", 0)
            value = update.get("value", "")

            field_stats[col]["count"] += 1
            field_stats[col]["values"].append(value)

            # Track confidence
            current_avg = field_stats[col]["avg_confidence"]
            current_count = field_stats[col]["count"]
            field_stats[col]["avg_confidence"] = (
                (current_avg * (current_count - 1) + confidence) / current_count
            )

    # Convert to regular dict and add summaries
    result = {}
    for col, stats in field_stats.items():
        result[col] = {
            "count": stats["count"],
            "avg_confidence": round(stats["avg_confidence"], 3),
            "unique_values": len(set(stats["values"])),
            "sample_values": list(set(stats["values"]))[:5]
        }

    return dict(sorted(result.items(), key=lambda x: x[1]["count"], reverse=True))


def analyze_events(results: List[Dict]) -> Dict[str, int]:
    """Analyze event type distribution."""
    event_counts = defaultdict(int)

    for r in results:
        for event in r.get("events", []):
            event_type = event.get("type", "unknown")
            event_counts[event_type] += 1

    return dict(sorted(event_counts.items(), key=lambda x: x[1], reverse=True))


def analyze_response_emails(results: List[Dict]) -> Dict[str, int]:
    """Analyze response email patterns."""
    stats = {
        "total_responses": 0,
        "no_response": 0,
        "asking_for_fields": 0,
        "closing_emails": 0,
        "asking_for_phone": 0,
        "other": 0
    }

    for r in results:
        response = r.get("response_email")

        if not response:
            stats["no_response"] += 1
            continue

        stats["total_responses"] += 1
        response_lower = response.lower()

        if "thank you" in response_lower or "thanks for" in response_lower:
            stats["closing_emails"] += 1
        elif "phone" in response_lower or "number to reach" in response_lower:
            stats["asking_for_phone"] += 1
        elif any(field in response_lower for field in ["sf", "square feet", "ceiling", "power", "dock"]):
            stats["asking_for_fields"] += 1
        else:
            stats["other"] += 1

    return stats


def analyze_issues(results: List[Dict]) -> List[Dict]:
    """Analyze common issues from failed tests."""
    issue_patterns = defaultdict(lambda: {"count": 0, "examples": []})

    for r in results:
        for issue in r.get("issues", []):
            # Normalize issue for grouping
            if "FORBIDDEN update" in issue:
                key = "Forbidden field update"
            elif "FORBIDDEN request" in issue:
                key = "Forbidden field request"
            elif "Missing expected event" in issue:
                key = "Missing expected event"
            elif "Expected no response email" in issue:
                key = "Unexpected response email"
            elif "Exception" in issue:
                key = "Runtime exception"
            else:
                key = issue[:50]

            issue_patterns[key]["count"] += 1
            if len(issue_patterns[key]["examples"]) < 3:
                issue_patterns[key]["examples"].append({
                    "test_id": r.get("test_id"),
                    "full_issue": issue
                })

    # Sort by count
    sorted_issues = sorted(
        [{"pattern": k, **v} for k, v in issue_patterns.items()],
        key=lambda x: x["count"],
        reverse=True
    )

    return sorted_issues


def analyze_latency_distribution(results: List[Dict]) -> Dict:
    """Analyze latency distribution in detail."""
    latencies = [r.get("duration_ms", 0) for r in results]

    if not latencies:
        return {}

    sorted_lat = sorted(latencies)
    n = len(sorted_lat)

    return {
        "min": sorted_lat[0],
        "max": sorted_lat[-1],
        "avg": round(sum(sorted_lat) / n),
        "p25": sorted_lat[int(n * 0.25)],
        "p50": sorted_lat[int(n * 0.50)],
        "p75": sorted_lat[int(n * 0.75)],
        "p90": sorted_lat[int(n * 0.90)],
        "p95": sorted_lat[int(n * 0.95)],
        "p99": sorted_lat[int(n * 0.99)] if n >= 100 else sorted_lat[-1],
        "under_1s": sum(1 for l in sorted_lat if l < 1000),
        "under_2s": sum(1 for l in sorted_lat if l < 2000),
        "under_5s": sum(1 for l in sorted_lat if l < 5000),
        "over_5s": sum(1 for l in sorted_lat if l >= 5000),
        "over_10s": sum(1 for l in sorted_lat if l >= 10000)
    }


def analyze_by_test_type(results: List[Dict]) -> Dict[str, Dict]:
    """Analyze results grouped by test type."""
    by_type = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0, "issues": []})

    for r in results:
        test_type = r.get("type", "unknown")
        by_type[test_type]["total"] += 1

        if r.get("passed", False):
            by_type[test_type]["passed"] += 1
        else:
            by_type[test_type]["failed"] += 1
            by_type[test_type]["issues"].extend(r.get("issues", []))

    # Calculate pass rates
    result = {}
    for test_type, stats in by_type.items():
        result[test_type] = {
            "total": stats["total"],
            "passed": stats["passed"],
            "failed": stats["failed"],
            "pass_rate": round(stats["passed"] / stats["total"] * 100, 1) if stats["total"] else 0,
            "sample_issues": stats["issues"][:3]
        }

    return dict(sorted(result.items(), key=lambda x: x[1]["pass_rate"]))


def compare_runs(results1: List[Dict], results2: List[Dict], summary1: Dict, summary2: Dict) -> Dict:
    """Compare two test runs."""
    comparison = {
        "run1": {
            "run_id": summary1.get("run_id", "unknown"),
            "total": summary1.get("total_tests", 0),
            "pass_rate": summary1.get("pass_rate", 0)
        },
        "run2": {
            "run_id": summary2.get("run_id", "unknown"),
            "total": summary2.get("total_tests", 0),
            "pass_rate": summary2.get("pass_rate", 0)
        },
        "pass_rate_change": summary2.get("pass_rate", 0) - summary1.get("pass_rate", 0),
        "latency_change": {
            "avg": summary2.get("avg_latency_ms", 0) - summary1.get("avg_latency_ms", 0),
            "p50": summary2.get("p50_latency_ms", 0) - summary1.get("p50_latency_ms", 0),
            "p90": summary2.get("p90_latency_ms", 0) - summary1.get("p90_latency_ms", 0)
        }
    }

    # Find tests that changed status
    results1_by_id = {r.get("test_id"): r for r in results1}
    results2_by_id = {r.get("test_id"): r for r in results2}

    newly_failing = []
    newly_passing = []

    for test_id, r2 in results2_by_id.items():
        if test_id in results1_by_id:
            r1 = results1_by_id[test_id]
            if r1.get("passed") and not r2.get("passed"):
                newly_failing.append({
                    "test_id": test_id,
                    "issues": r2.get("issues", [])
                })
            elif not r1.get("passed") and r2.get("passed"):
                newly_passing.append(test_id)

    comparison["newly_failing"] = newly_failing
    comparison["newly_passing"] = newly_passing
    comparison["regressions"] = len(newly_failing)
    comparison["fixes"] = len(newly_passing)

    return comparison


def generate_html_report(analysis: Dict, output_path: str):
    """Generate an HTML report from analysis results."""
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Test Results Analysis - {analysis.get('run_id', 'Unknown')}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #007bff; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        .pass {{ color: #28a745; }}
        .fail {{ color: #dc3545; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        .progress-bar {{ width: 100%; height: 8px; background: #e9ecef; border-radius: 4px; overflow: hidden; }}
        .progress-fill {{ height: 100%; background: #28a745; }}
        .issue-list {{ background: #fff3cd; padding: 15px; border-radius: 4px; margin: 10px 0; }}
        .latency-dist {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .latency-bucket {{ padding: 10px 15px; background: #e9ecef; border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Test Results Analysis</h1>
        <p>Run ID: {analysis.get('run_id', 'Unknown')} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="summary">
            <div class="stat-card">
                <div class="stat-value">{analysis.get('total_tests', 0)}</div>
                <div class="stat-label">Total Tests</div>
            </div>
            <div class="stat-card">
                <div class="stat-value pass">{analysis.get('passed', 0)}</div>
                <div class="stat-label">Passed</div>
            </div>
            <div class="stat-card">
                <div class="stat-value fail">{analysis.get('failed', 0)}</div>
                <div class="stat-label">Failed</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{analysis.get('pass_rate', 0):.1f}%</div>
                <div class="stat-label">Pass Rate</div>
            </div>
        </div>

        <h2>Performance</h2>
        <div class="latency-dist">
            <div class="latency-bucket">Avg: {analysis.get('latency', {}).get('avg', 0)}ms</div>
            <div class="latency-bucket">P50: {analysis.get('latency', {}).get('p50', 0)}ms</div>
            <div class="latency-bucket">P90: {analysis.get('latency', {}).get('p90', 0)}ms</div>
            <div class="latency-bucket">P99: {analysis.get('latency', {}).get('p99', 0)}ms</div>
        </div>

        <h2>Results by Category</h2>
        <table>
            <tr><th>Category</th><th>Total</th><th>Passed</th><th>Failed</th><th>Pass Rate</th></tr>
"""

    for cat, stats in analysis.get('by_category', {}).items():
        rate = stats.get('passed', 0) / stats.get('total', 1) * 100
        html += f"""            <tr>
                <td>{cat}</td>
                <td>{stats.get('total', 0)}</td>
                <td class="pass">{stats.get('passed', 0)}</td>
                <td class="fail">{stats.get('failed', 0)}</td>
                <td>
                    <div class="progress-bar"><div class="progress-fill" style="width: {rate}%"></div></div>
                    {rate:.1f}%
                </td>
            </tr>
"""

    html += """        </table>

        <h2>Field Extraction Stats</h2>
        <table>
            <tr><th>Field</th><th>Count</th><th>Avg Confidence</th><th>Sample Values</th></tr>
"""

    for field, stats in analysis.get('field_stats', {}).items():
        samples = ", ".join(str(v) for v in stats.get('sample_values', [])[:3])
        html += f"""            <tr>
                <td>{field}</td>
                <td>{stats.get('count', 0)}</td>
                <td>{stats.get('avg_confidence', 0):.2f}</td>
                <td>{samples}</td>
            </tr>
"""

    html += """        </table>

        <h2>Event Distribution</h2>
        <table>
            <tr><th>Event Type</th><th>Count</th></tr>
"""

    for event_type, count in analysis.get('event_stats', {}).items():
        html += f"""            <tr><td>{event_type}</td><td>{count}</td></tr>
"""

    html += """        </table>
"""

    if analysis.get('common_issues'):
        html += """
        <h2>Common Issues</h2>
"""
        for issue in analysis.get('common_issues', [])[:10]:
            html += f"""        <div class="issue-list">
            <strong>{issue.get('pattern', 'Unknown')} ({issue.get('count', 0)} occurrences)</strong>
            <ul>
"""
            for example in issue.get('examples', []):
                html += f"""                <li>{example.get('test_id', '')}: {example.get('full_issue', '')}</li>
"""
            html += """            </ul>
        </div>
"""

    html += """    </div>
</body>
</html>
"""

    with open(output_path, 'w') as f:
        f.write(html)

    print(f"HTML report saved to: {output_path}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Analyze batch test results")
    parser.add_argument("--results", "-r", required=True, help="Results directory to analyze")
    parser.add_argument("--compare", "-c", help="Compare with another results directory")
    parser.add_argument("--export-html", help="Export HTML report to file")
    parser.add_argument("--export-json", help="Export analysis as JSON")
    args = parser.parse_args()

    results_path = Path(args.results)

    if not results_path.exists():
        print(f"Results directory not found: {results_path}")
        sys.exit(1)

    # Load results
    print(f"Loading results from: {results_path}")
    summary, all_results = load_results(results_path)

    if not all_results:
        print("No results found!")
        sys.exit(1)

    print(f"Analyzing {len(all_results)} test results...\n")

    # Run analysis
    analysis = {
        "run_id": summary.get("run_id", "unknown"),
        "total_tests": summary.get("total_tests", len(all_results)),
        "passed": summary.get("passed", sum(1 for r in all_results if r.get("passed"))),
        "failed": summary.get("failed", sum(1 for r in all_results if not r.get("passed"))),
        "pass_rate": summary.get("pass_rate", 0),
        "by_category": summary.get("by_category", {}),
        "latency": analyze_latency_distribution(all_results),
        "field_stats": analyze_field_extraction(all_results),
        "event_stats": analyze_events(all_results),
        "response_stats": analyze_response_emails(all_results),
        "common_issues": analyze_issues(all_results),
        "by_test_type": analyze_by_test_type(all_results)
    }

    # Print summary
    print("=" * 60)
    print("ANALYSIS RESULTS")
    print("=" * 60)
    print(f"Run ID: {analysis['run_id']}")
    print(f"Total: {analysis['total_tests']} | Passed: {analysis['passed']} | Failed: {analysis['failed']}")
    print(f"Pass Rate: {analysis['pass_rate']:.1f}%")

    print(f"\n--- Latency Distribution ---")
    lat = analysis['latency']
    print(f"  Min: {lat.get('min', 0)}ms | Max: {lat.get('max', 0)}ms")
    print(f"  P25: {lat.get('p25', 0)}ms | P50: {lat.get('p50', 0)}ms | P75: {lat.get('p75', 0)}ms")
    print(f"  P90: {lat.get('p90', 0)}ms | P95: {lat.get('p95', 0)}ms | P99: {lat.get('p99', 0)}ms")
    print(f"  Under 1s: {lat.get('under_1s', 0)} | Under 2s: {lat.get('under_2s', 0)} | Over 5s: {lat.get('over_5s', 0)}")

    print(f"\n--- Field Extraction ---")
    for field, stats in list(analysis['field_stats'].items())[:10]:
        print(f"  {field}: {stats['count']} extractions (avg conf: {stats['avg_confidence']:.2f})")

    print(f"\n--- Event Distribution ---")
    for event_type, count in analysis['event_stats'].items():
        print(f"  {event_type}: {count}")

    print(f"\n--- Response Emails ---")
    for stat_name, count in analysis['response_stats'].items():
        print(f"  {stat_name}: {count}")

    if analysis['common_issues']:
        print(f"\n--- Common Issues ---")
        for issue in analysis['common_issues'][:5]:
            print(f"  {issue['pattern']}: {issue['count']} occurrences")

    print(f"\n--- Results by Test Type ---")
    for test_type, stats in analysis['by_test_type'].items():
        status = "✓" if stats['pass_rate'] == 100 else "✗" if stats['pass_rate'] < 80 else "~"
        print(f"  {status} {test_type}: {stats['pass_rate']:.1f}% ({stats['passed']}/{stats['total']})")

    # Compare runs if requested
    if args.compare:
        compare_path = Path(args.compare)
        if compare_path.exists():
            print(f"\n--- Comparing with: {args.compare} ---")
            summary2, results2 = load_results(compare_path)
            comparison = compare_runs(all_results, results2, summary, summary2)

            print(f"  Pass rate change: {comparison['pass_rate_change']:+.1f}%")
            print(f"  Regressions: {comparison['regressions']}")
            print(f"  Fixes: {comparison['fixes']}")

            if comparison['newly_failing']:
                print(f"\n  Newly failing tests:")
                for fail in comparison['newly_failing'][:5]:
                    print(f"    - {fail['test_id']}")

    # Export HTML if requested
    if args.export_html:
        generate_html_report(analysis, args.export_html)

    # Export JSON if requested
    if args.export_json:
        with open(args.export_json, 'w') as f:
            json.dump(analysis, f, indent=2)
        print(f"\nJSON analysis saved to: {args.export_json}")


if __name__ == "__main__":
    main()
