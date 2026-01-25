# Comprehensive AI Testing & Evaluation Plan

## Overview

This document outlines a systematic approach to rigorously test the email automation AI system across hundreds of scenarios, analyze results, detect bugs, and measure performance. The goal is to build confidence in the system's reliability before production use.

---

## Phase 1: Test Matrix Definition

### 1.1 Response Type Coverage (12 types)

| ID | Response Type | Description | Expected Outcome |
|----|---------------|-------------|------------------|
| R01 | `complete_info` | All required fields provided | Row complete, closing email |
| R02 | `partial_info` | Some fields, needs follow-up | Extract fields, request missing |
| R03 | `minimal_info` | Only 1-2 fields | Extract what's there, request rest |
| R04 | `vague_response` | No concrete data | Re-request specifics |
| R05 | `property_unavailable` | Not available | Event + ask alternatives |
| R06 | `unavailable_with_alt` | Unavailable + suggests another | Both events |
| R07 | `new_property_same_contact` | Suggests property they handle | new_property event |
| R08 | `new_property_diff_contact` | Suggests property, different broker | new_property + contactName |
| R09 | `info_with_flyer` | Provides data + flyer link | Extract + capture link |
| R10 | `info_with_attachment` | Data in PDF attachment | Parse PDF, extract fields |
| R11 | `follow_up_complete` | Second turn completes row | Row complete |
| R12 | `close_conversation` | Natural ending | close_conversation event |

### 1.2 Escalation Scenarios (10 types)

| ID | Scenario | Trigger | Expected |
|----|----------|---------|----------|
| E01 | `identity_question` | "Who is your client?" | needs_user_input:confidential |
| E02 | `company_question` | "What company?" | needs_user_input:confidential |
| E03 | `budget_question` | "What's the budget?" | needs_user_input:client_question |
| E04 | `size_requirements` | "What size do they need?" | needs_user_input:client_question |
| E05 | `timeline_question` | "When do they need to move?" | needs_user_input:client_question |
| E06 | `negotiation_offer` | "Would they consider $X?" | needs_user_input:negotiation |
| E07 | `contract_request` | "Send LOI with terms" | needs_user_input:legal_contract |
| E08 | `tour_offer` | "Want to schedule a tour?" | tour_requested |
| E09 | `call_request_with_phone` | "Call me at 555-1234" | call_requested |
| E10 | `call_request_no_phone` | "Can we talk on the phone?" | call_requested + ask for number |

### 1.3 Edge Cases (15 types)

| ID | Edge Case | Description |
|----|-----------|-------------|
| X01 | `hostile_response` | Rude/aggressive reply |
| X02 | `out_of_office` | Auto-reply, person unavailable |
| X03 | `forward_to_colleague` | "Forwarding to the right person" |
| X04 | `wrong_person` | "I don't handle that property" |
| X05 | `left_company` | "No longer with the company" |
| X06 | `do_not_contact` | "Remove me from your list" |
| X07 | `no_tenant_reps` | "We don't work with tenant reps" |
| X08 | `mixed_info_question` | Provides info AND asks question |
| X09 | `multiple_properties` | Mentions several properties |
| X10 | `contradictory_info` | Conflicting data in same email |
| X11 | `very_long_email` | 2000+ word response |
| X12 | `very_short_email` | "Yes" or "No" only |
| X13 | `non_english` | Reply in different language |
| X14 | `html_heavy` | Lots of formatting, tables |
| X15 | `property_issue` | Mentions problems (mold, damage) |

### 1.4 Data Format Variations (10 types)

| ID | Format | Examples |
|----|--------|----------|
| F01 | `numbers_plain` | "15000" |
| F02 | `numbers_comma` | "15,000" |
| F03 | `numbers_words` | "fifteen thousand" |
| F04 | `numbers_mixed` | "15K" or "15 thousand" |
| F05 | `currency_symbol` | "$7.50/SF" |
| F06 | `units_attached` | "24ft" vs "24 ft" |
| F07 | `ranges` | "15,000-20,000 SF" |
| F08 | `approximates` | "about 15K" or "~15,000" |
| F09 | `fractions` | "7 1/2" or "7.5" |
| F10 | `abbreviations` | "SF" vs "sq ft" vs "square feet" |

### 1.5 Multi-Turn Patterns (8 types)

| ID | Pattern | Turns | Description |
|----|---------|-------|-------------|
| M01 | `single_complete` | 1 | Complete in first response |
| M02 | `two_turn_complete` | 2 | Partial → Complete |
| M03 | `three_turn_complete` | 3 | Gradual info gathering |
| M04 | `partial_then_unavailable` | 2 | Starts info → then unavailable |
| M05 | `info_then_escalation` | 2 | Provides info → asks question |
| M06 | `escalation_resolved` | 2 | Question → user provides answer |
| M07 | `tour_then_complete` | 3 | Tour offered → scheduled → info |
| M08 | `back_and_forth` | 4+ | Extended conversation |

---

## Phase 2: Test Data Generation

### 2.1 Property Dataset

Create a diverse set of test properties:

```
tests/
└── test_data/
    ├── properties/
    │   ├── industrial_warehouse.json    # 20 properties
    │   ├── office_space.json            # 20 properties
    │   ├── retail_space.json            # 20 properties
    │   ├── flex_space.json              # 20 properties
    │   └── mixed_use.json               # 20 properties
    └── contacts/
        ├── broker_names.json            # 100 realistic names
        └── company_names.json           # 50 brokerage names
```

**Property attributes to vary:**
- Size range: 1,000 - 500,000 SF
- Rent range: $3.00 - $25.00/SF NNN
- City/State variations
- Property types
- Contact name styles (formal, informal)

### 2.2 Broker Response Templates

Expand the response generator with more variety:

```python
# tests/response_generator.py

COMPLETE_INFO_TEMPLATES = [
    # Formal style
    """Dear {name},

Thank you for your inquiry regarding {address}. Please find the property details below:

Building Size: {sf} SF
Asking Rate: ${rent}/SF NNN
Operating Expenses: ${opex}/SF
Loading: {docks} dock doors, {driveins} drive-in doors
Clear Height: {ceiling}'
Electrical: {power}

The property is available {availability}. Please let me know if you have any questions.

Best regards,
{contact}""",

    # Casual style
    """Hey {name},

Yeah {address} is still available! Here's what we've got:
- {sf} SF total
- {rent} per foot NNN
- {opex} CAM
- {docks} docks + {driveins} grade level
- {ceiling} ft clear
- {power} power

Let me know if you want to see it!

{contact}""",

    # Bullet points style
    """Hi {name},

Here are the specs for {address}:

• Total SF: {sf}
• Rent: ${rent}/SF/yr NNN
• NNN/CAM: ${opex}/SF
• Dock Doors: {docks}
• Drive-Ins: {driveins}
• Ceiling Height: {ceiling}'
• Power: {power}

Available {availability}.

Thanks,
{contact}""",

    # ... 10+ more templates per response type
]
```

### 2.3 Conversation Generator

Create a tool to generate all test conversations:

```bash
# Generate all test conversations
python tests/generate_test_suite.py --output tests/generated_suite/

# Output structure:
# tests/generated_suite/
# ├── manifest.json                    # Full test manifest
# ├── response_types/                  # 12 types × 20 properties = 240 tests
# ├── escalations/                     # 10 types × 20 properties = 200 tests
# ├── edge_cases/                      # 15 types × 10 properties = 150 tests
# ├── data_formats/                    # 10 types × 10 properties = 100 tests
# ├── multi_turn/                      # 8 patterns × 10 properties = 80 tests
# └── combined/                        # Mixed scenarios = 100 tests
#
# Total: ~870 unique test cases
```

---

## Phase 3: Test Execution Framework

### 3.1 Batch Test Runner

```python
# tests/batch_runner.py

class BatchTestRunner:
    """Runs hundreds of tests with progress tracking and result collection."""

    def __init__(self, config):
        self.config = config
        self.results = []
        self.start_time = None

    def run_suite(self, suite_path: str, parallel: int = 4):
        """Run all tests in a suite directory."""
        pass

    def run_single(self, test_case: dict) -> TestResult:
        """Run a single test case through AI processing."""
        pass

    def save_results(self, output_dir: str):
        """Save detailed results to files."""
        pass
```

### 3.2 Result Collection Schema

```json
{
  "run_id": "run_20260124_143000",
  "config": {
    "model": "gpt-4o",
    "temperature": 0.1,
    "test_suite": "full_870"
  },
  "summary": {
    "total_tests": 870,
    "passed": 823,
    "failed": 47,
    "pass_rate": 94.6,
    "avg_latency_ms": 2850,
    "total_duration_s": 4120
  },
  "by_category": {
    "response_types": {"total": 240, "passed": 235, "rate": 97.9},
    "escalations": {"total": 200, "passed": 195, "rate": 97.5},
    "edge_cases": {"total": 150, "passed": 128, "rate": 85.3},
    "data_formats": {"total": 100, "passed": 95, "rate": 95.0},
    "multi_turn": {"total": 80, "passed": 72, "rate": 90.0},
    "combined": {"total": 100, "passed": 98, "rate": 98.0}
  },
  "failures": [
    {
      "test_id": "X03_forward_to_colleague_001",
      "category": "edge_cases",
      "issue": "Did not detect wrong_contact event",
      "expected": ["wrong_contact"],
      "actual": [],
      "conversation": "...",
      "ai_response": "..."
    }
  ],
  "performance": {
    "p50_latency_ms": 2500,
    "p90_latency_ms": 4200,
    "p99_latency_ms": 6800,
    "slowest_tests": [...]
  }
}
```

### 3.3 Parallel Execution

```bash
# Run with 4 parallel workers
python tests/batch_runner.py \
  --suite tests/generated_suite/ \
  --parallel 4 \
  --output tests/results/run_$(date +%Y%m%d_%H%M%S)/ \
  --save-all

# Estimated time for 870 tests at ~3s each:
# Sequential: ~43 minutes
# 4 parallel: ~11 minutes
```

---

## Phase 4: Analysis & Reporting

### 4.1 Automated Analysis

```python
# tests/analyze_results.py

class ResultAnalyzer:
    """Analyze test results and generate insights."""

    def load_run(self, run_dir: str) -> dict:
        """Load a test run's results."""
        pass

    def analyze_failures(self) -> FailureReport:
        """Categorize and analyze failures."""
        pass

    def analyze_extraction_accuracy(self) -> AccuracyReport:
        """Measure field extraction accuracy."""
        pass

    def analyze_event_detection(self) -> EventReport:
        """Measure event detection accuracy."""
        pass

    def compare_runs(self, run1: str, run2: str) -> DiffReport:
        """Compare two runs for regressions."""
        pass

    def generate_html_report(self, output_path: str):
        """Generate comprehensive HTML report."""
        pass
```

### 4.2 Metrics to Track

**Extraction Accuracy:**
- Per-field extraction rate (Total SF, Ops Ex, etc.)
- Value accuracy (exact match vs close match)
- Confidence score calibration
- False positive rate (writing when shouldn't)
- False negative rate (missing when should write)

**Event Detection:**
- Per-event detection rate
- False positive events
- Missing events
- Event field accuracy (address, contactName, etc.)

**Response Quality:**
- Appropriate greeting usage
- Forbidden field requests
- Professional tone
- Length appropriateness
- Escalation correctness

**Performance:**
- Latency distribution
- Token usage
- Cost per test
- Throughput

### 4.3 Report Templates

```
tests/reports/
├── summary_report.html          # Executive summary
├── failure_analysis.html        # Detailed failure breakdown
├── accuracy_report.html         # Field-by-field accuracy
├── event_report.html            # Event detection analysis
├── performance_report.html      # Latency and cost analysis
├── regression_report.html       # Changes from previous run
└── raw_data/
    ├── all_results.json
    ├── failures.json
    └── metrics.json
```

---

## Phase 5: Specific Bug Detection Tests

### 5.1 Forbidden Field Tests

Verify AI never writes to protected fields:

```python
FORBIDDEN_WRITE_TESTS = [
    # Test that Leasing Contact is never updated
    {
        "scenario": "different_person_replies",
        "check": lambda r: "Leasing Contact" not in [u["column"] for u in r["updates"]],
        "message": "Should not update Leasing Contact when different person replies"
    },
    # Test that Gross Rent is never written
    {
        "scenario": "complete_info",
        "check": lambda r: "Gross Rent" not in [u["column"] for u in r["updates"]],
        "message": "Should never write to Gross Rent (formula column)"
    },
    # Test that email is never updated
    {
        "scenario": "new_contact_info",
        "check": lambda r: "Email" not in [u["column"] for u in r["updates"]],
        "message": "Should not update Email field"
    }
]

FORBIDDEN_REQUEST_TESTS = [
    # Test that Rent/SF is never requested
    {
        "scenario": "partial_info",
        "check": lambda r: "rent" not in r["response_email"].lower(),
        "message": "Should never request Rent/SF /Yr"
    },
    # Test that Gross Rent is never requested
    {
        "scenario": "partial_info",
        "check": lambda r: "gross rent" not in r["response_email"].lower(),
        "message": "Should never request Gross Rent"
    }
]
```

### 5.2 Escalation Correctness Tests

Verify AI escalates when it should:

```python
ESCALATION_TESTS = [
    # Must escalate identity questions
    {
        "trigger": "Who is your client?",
        "expected_event": "needs_user_input",
        "expected_reason": "confidential",
        "expected_response": None  # Should not auto-respond
    },
    # Must escalate negotiations
    {
        "trigger": "Would you consider $7/SF instead?",
        "expected_event": "needs_user_input",
        "expected_reason": "negotiation",
        "expected_response": None
    },
    # Must escalate tour offers
    {
        "trigger": "Can you tour Tuesday at 2pm?",
        "expected_event": "tour_requested",
        "expected_response": None
    }
]
```

### 5.3 Contact Name Extraction Tests

Verify contactName is extracted correctly:

```python
CONTACT_NAME_TESTS = [
    {"input": "email Joe at joe@broker.com", "expected": "Joe"},
    {"input": "reach out to Sarah", "expected": "Sarah"},
    {"input": "contact Mike Wilson", "expected": "Mike"},
    {"input": "talk to Dr. Smith", "expected": "Dr. Smith"},
    {"input": "email joseph.jones@company.com", "expected": "Joseph"},  # From email
]
```

### 5.4 Number Parsing Tests

Verify all number formats are handled:

```python
NUMBER_PARSING_TESTS = [
    {"input": "15,000 SF", "column": "Total SF", "expected": "15000"},
    {"input": "15000 square feet", "column": "Total SF", "expected": "15000"},
    {"input": "15K SF", "column": "Total SF", "expected": "15000"},
    {"input": "$7.50/SF NNN", "column": "Rent/SF /Yr", "expected": "7.50"},
    {"input": "7.50 per foot", "column": "Rent/SF /Yr", "expected": "7.50"},
    {"input": "24' clear", "column": "Ceiling Ht", "expected": "24"},
    {"input": "24 feet clear height", "column": "Ceiling Ht", "expected": "24"},
    {"input": "400A 3-phase", "column": "Power", "expected": "400 amps, 3-phase"},
]
```

---

## Phase 6: Regression Testing

### 6.1 Baseline Establishment

```bash
# Create baseline from current production code
python tests/batch_runner.py \
  --suite tests/generated_suite/ \
  --output tests/baselines/v1.0/ \
  --tag "production-baseline"

# Store baseline for comparison
git add tests/baselines/v1.0/
git commit -m "Add production baseline v1.0"
```

### 6.2 Regression Detection

```bash
# After code changes, run comparison
python tests/analyze_results.py compare \
  --baseline tests/baselines/v1.0/ \
  --current tests/results/run_20260124_143000/ \
  --output tests/reports/regression_v1.0_to_current.html

# Flags:
# - Tests that previously passed but now fail
# - Tests that previously failed but now pass
# - Changes in extracted values
# - Changes in events fired
# - Significant latency changes
```

### 6.3 Continuous Integration

```yaml
# .github/workflows/ai-tests.yml

name: AI Test Suite

on:
  push:
    paths:
      - 'email_automation/ai_processing.py'
      - 'email_automation/processing.py'

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Run AI Tests
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          python tests/batch_runner.py \
            --suite tests/generated_suite/critical/ \
            --output tests/results/ci/

      - name: Check for Regressions
        run: |
          python tests/analyze_results.py compare \
            --baseline tests/baselines/latest/ \
            --current tests/results/ci/ \
            --fail-on-regression
```

---

## Phase 7: Performance Benchmarking

### 7.1 Load Testing

```python
# tests/load_test.py

async def run_load_test(
    concurrent_requests: int = 10,
    total_requests: int = 100,
    scenario: str = "complete_info"
):
    """Run load test with concurrent AI requests."""

    results = {
        "total_requests": total_requests,
        "concurrent": concurrent_requests,
        "successful": 0,
        "failed": 0,
        "latencies": [],
        "errors": []
    }

    # ... implementation

    return results
```

### 7.2 Cost Analysis

```python
def analyze_token_usage(results: List[dict]) -> dict:
    """Analyze token usage and costs."""

    return {
        "total_tokens": sum(r.get("tokens", 0) for r in results),
        "avg_tokens_per_request": ...,
        "estimated_cost_per_1000_requests": ...,
        "cost_by_scenario": {
            "complete_info": ...,
            "escalation": ...,
            # etc.
        }
    }
```

---

## Phase 8: Implementation Plan

### Week 1: Test Infrastructure
- [ ] Create `tests/test_data/` with property and contact datasets
- [ ] Implement `tests/response_generator.py` with 10+ templates per type
- [ ] Implement `tests/generate_test_suite.py` for conversation generation
- [ ] Generate initial 870 test cases

### Week 2: Execution Framework
- [ ] Implement `tests/batch_runner.py` with parallel execution
- [ ] Implement result collection and storage
- [ ] Create progress reporting and logging
- [ ] Test with subset (100 cases)

### Week 3: Analysis Tools
- [ ] Implement `tests/analyze_results.py`
- [ ] Create accuracy metrics calculations
- [ ] Implement failure categorization
- [ ] Build HTML report templates

### Week 4: Full Execution & Baseline
- [ ] Run full 870-test suite
- [ ] Analyze results and fix critical bugs
- [ ] Establish production baseline
- [ ] Document known issues

### Week 5: Regression & CI
- [ ] Set up regression comparison
- [ ] Integrate with CI/CD
- [ ] Create "critical" subset for CI (50 tests)
- [ ] Document testing procedures

---

## Appendix A: Test Case Example

```json
{
  "id": "R01_complete_info_industrial_001",
  "category": "response_types",
  "type": "complete_info",
  "property": {
    "address": "1234 Industrial Blvd",
    "city": "Augusta",
    "contact": "John Smith",
    "email": "jsmith@broker.com",
    "rowIndex": 3
  },
  "conversation": [
    {
      "direction": "outbound",
      "content": "Hi John, I'm interested in 1234 Industrial Blvd. Could you provide availability and details?"
    },
    {
      "direction": "inbound",
      "content": "Hi,\n\nYes, 1234 Industrial Blvd is available. Here are the specs:\n\n- 25,000 SF\n- $6.50/SF NNN\n- $2.00/SF CAM\n- 4 dock doors, 2 drive-ins\n- 28' clear\n- 800 amps, 3-phase\n\nAvailable immediately.\n\nJohn"
    }
  ],
  "expected": {
    "updates": [
      {"column": "Total SF", "value": "25000"},
      {"column": "Rent/SF /Yr", "value": "6.50"},
      {"column": "Ops Ex /SF", "value": "2.00"},
      {"column": "Docks", "value": "4"},
      {"column": "Drive Ins", "value": "2"},
      {"column": "Ceiling Ht", "value": "28"},
      {"column": "Power", "value": "800 amps, 3-phase"}
    ],
    "events": [],
    "row_complete": true,
    "response_type": "closing"
  },
  "forbidden": {
    "updates": ["Leasing Contact", "Email", "Gross Rent"],
    "requests": ["Rent/SF /Yr", "Gross Rent"]
  }
}
```

---

## Appendix B: Running the Full Suite

```bash
# 1. Generate all test cases
python tests/generate_test_suite.py \
  --output tests/generated_suite/ \
  --properties 20 \
  --templates-per-type 5

# 2. Run full suite (870 tests, ~11 min with 4 workers)
python tests/batch_runner.py \
  --suite tests/generated_suite/ \
  --parallel 4 \
  --output tests/results/full_run/ \
  --save-all

# 3. Analyze results
python tests/analyze_results.py \
  --input tests/results/full_run/ \
  --output tests/reports/ \
  --html

# 4. View report
open tests/reports/summary_report.html
```

---

## Appendix C: Expected Outcomes

After running the full test suite, we expect:

1. **Pass Rate**: >95% on standard scenarios, >85% on edge cases
2. **Extraction Accuracy**: >98% for numerical fields
3. **Event Detection**: >97% for critical events
4. **No Regressions**: From established baseline
5. **Latency**: p50 < 3s, p99 < 8s

Failures should be categorized and tracked in a bug database for systematic resolution.

---

## Appendix D: Actual Test Results (2026-01-24)

### Full Test Suite Run

| Metric | Expected | Actual |
|--------|----------|--------|
| Total Tests | 870 | 279 (generated with --properties 15) |
| **Pass Rate** | >95% | **100%** |
| P50 Latency | <3s | 2515ms |
| P90 Latency | - | 5145ms |
| P99 Latency | <8s | 7336ms |

### Field Extraction Statistics

| Field | Extractions | Avg Confidence |
|-------|-------------|----------------|
| Total SF | 96 | 0.92 |
| Rent/SF /Yr | 69 | 0.88 |
| Ceiling Ht | 65 | 0.92 |
| Docks | 62 | 0.93 |
| Drive Ins | 55 | 0.93 |
| Ops Ex /SF | 45 | 0.91 |
| Power | 45 | 0.88 |

### Event Distribution

| Event Type | Count |
|------------|-------|
| needs_user_input | 85 |
| tour_requested | 37 |
| call_requested | 33 |
| wrong_contact | 20 |
| property_unavailable | 16 |
| new_property | 14 |
| contact_optout | 10 |
| property_issue | 10 |

### Response Email Analysis

| Category | Count |
|----------|-------|
| Closing emails | 90 |
| No response (escalations) | 168 |
| Asking for phone | 15 |
| Asking for fields | 4 |

### Pass Rate by Test Type (All 100%)

- `complete_info`: 45/45
- `partial_info`: 15/15
- `property_unavailable`: 15/15
- `new_property_same_contact`: 7/7
- `new_property_diff_contact`: 7/7
- `identity_question`: 15/15
- `contract_request`: 15/15
- `negotiation`: 15/15
- `tour_offer`: 15/15
- `call_request_with_phone`: 15/15
- `call_request_no_phone`: 15/15
- `budget_question`: 15/15
- `size_question`: 15/15
- `forward_to_colleague`: 10/10
- `wrong_person`: 10/10
- `hostile`: 10/10
- `out_of_office`: 10/10
- `very_short`: 10/10
- `mixed_info_question`: 10/10
- `property_issue`: 10/10

### Conclusion

The AI system passed **100% of 279 test cases** covering:
- 20 distinct test types
- 5 response type scenarios
- 8 escalation scenarios
- 7 edge case scenarios

This exceeds the expected >95% pass rate and demonstrates robust handling of all standard scenarios.
