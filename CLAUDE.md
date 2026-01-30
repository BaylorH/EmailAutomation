# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Related Repository

**Frontend:** `~/Documents/GitHub/email-admin-ui` (React admin dashboard)

This is the **backend** - it processes emails, extracts data via AI, and updates sheets. The frontend handles user interaction, client management, and queues emails to Firestore for this backend to process.

## Full System Architecture

```
Frontend (email-admin-ui)          Backend (this repo)
        │                                  │
        │ Writes to outbox/                │ Reads outbox, sends emails
        │ Manages clients/                 │ Processes inbox replies
        │ Reads notifications/             │ Writes notifications/
        │                                  │ Updates Google Sheets
        └──────────► FIRESTORE ◄───────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
        Microsoft Graph        OpenAI GPT-4o
        (send/receive)         (extraction)
```

**Data ownership:**
- Frontend writes: `users/{uid}`, `clients/`, `outbox/`
- Backend writes: `threads/`, `msgIndex/`, `convIndex/`, `notifications/`
- Backend consumes: `outbox/` (then deletes after sending)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run scheduled email processing (main entry point)
python main.py

# Run Flask web server (OAuth UI + APIs)
python app.py

# Alternative scheduler with extended features
python scheduler_runner.py
```

**Automated Execution:** GitHub Actions runs email processing every 30 minutes (`.github/workflows/email.yml`)

## Architecture

```
Entry Points:
  main.py              → Runs processing for all users
  app.py               → Flask server for OAuth + APIs
  scheduler_runner.py  → Alternative extended scheduler

Core Pipeline (email_automation/):
  1. email.py            → Outbox processing, send drafts, follow-up scheduling
  2. processing.py       → Inbox scanning, thread matching, event handling (main logic)
  3. ai_processing.py    → OpenAI extraction, field validation, event detection
  4. sheets.py           → Google Sheets read/write operations
  5. messaging.py        → Firestore thread/message storage and indexing

Support Modules:
  clients.py           → Firestore & Google API client init
  email_operations.py  → Specialized email sending (replies, closing, follow-ups)
  sheet_operations.py  → NON-VIABLE divider, row movement, new property insertion
  utils.py             → Retry logic, HTML parsing, encoding helpers
  notifications.py     → User notifications (sheet_update, action_needed, etc.)
  file_handling.py     → PDF attachment handling, Google Drive upload

Configuration & Infrastructure:
  app_config.py        → Azure/Firebase/OpenAI config constants, E2E test mode flag
  column_config.py     → Dynamic column mapping, canonical field definitions, extraction rules
  followup.py          → Automatic follow-up emails for non-responsive brokers
  logging.py           → Google Sheets "Log" tab management, message processing history
  service_providers.py → Abstraction layer for external services (prod vs test mode)
```

## Data Flow

1. Frontend queues email in `users/{uid}/outbox/`
2. `email.py` reads outbox, sends via Microsoft Graph, indexes message in Firestore
3. Client replies arrive in inbox
4. `processing.py` matches reply to indexed thread via message/conversation ID
5. `ai_processing.py` calls OpenAI to extract property fields
6. `sheets.py` writes extracted data to Google Sheets
7. `messaging.py` writes notification to `users/{uid}/clients/{clientId}/notifications/`
8. Frontend picks up notification in real-time via `onSnapshot()`

## Firestore Structure (per user: `users/{uid}/`)

Written by backend:
- `threads/` - Thread root documents
- `threads/{id}/messages/` - Individual messages in thread
- `msgIndex/` - Message ID → thread lookup (O(1) discovery)
- `convIndex/` - Conversation ID → thread fallback

Written by frontend, read by backend:
- `clients/` - Client metadata (name, emails, criteria, sheetId)
- `outbox/` - Queued emails to send

Written by backend, read by frontend:
- `notifications/` - Per-client notifications (sheet_update, action_needed, row_completed, property_unavailable, conversation_closed)

## Key External Services

- **Microsoft Graph API** - Email send/receive via MSAL OAuth
- **OpenAI GPT-4o** - Email parsing and field extraction
- **Google Sheets API** - Property data storage
- **Firebase/Firestore** - Thread storage, token caching
- **Google Drive** - PDF archival

## Environment Variables

Azure: `AZURE_API_APP_ID`, `AZURE_API_CLIENT_SECRET`, `AZURE_TENANT_ID`
Firebase: `FIREBASE_API_KEY`, `FIREBASE_SA_KEY`
Google: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
OpenAI: `OPENAI_API_KEY`, `OPENAI_ASSISTANT_MODEL`

## Flask API Endpoints

### Authentication
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/auth/login` | GET | Initiate Microsoft OAuth flow via MSAL |
| `/auth/callback` | GET | OAuth callback, save token to Firestore |

### Core API
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Home page: token status, OAuth setup UI |
| `/api/status` | GET | Check if Microsoft Graph token is valid |
| `/api/upload` | POST | Upload MSAL token cache to Firebase Storage |
| `/api/clear` | POST | Clear local MSAL token cache file |
| `/api/refresh` | POST | Force refresh Microsoft Graph access token |
| `/api/trigger-scheduler` | POST | Manually trigger email processing |
| `/api/scheduler-status` | GET | Get scheduler status, last run result |

### Property & Sheet Management
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/decline-property` | POST | Delete property row when user rejects suggestion |
| `/api/accept-new-property` | POST | Create new property row when user accepts suggestion |
| `/api/check-sheet-completion` | POST | Check required field completion percentage |
| `/api/clear-optout` | POST | Remove email from opt-out list |
| `/api/list-optouts` | POST | Retrieve all opted-out contacts for user |

### Debug
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/debug-inbox` | GET | Fetch recent inbox emails, check processing status |
| `/api/debug-thread-matching` | GET | Test conversation/message ID matching logic |
| `/api/firestore-inspect` | GET | Inspect Firestore database structure |
| `/api/firestore-cleanup` | POST | Clean up test/malformed data in Firestore |

## Deployment

Hosted on Render.com (`https://email-token-manager.onrender.com`).

---

## Development Workflow (MUST FOLLOW)

### For Frontend Changes (email-admin-ui)

```
1. MAKE CHANGES
   └─> Edit files in src/, styles/, components/

2. BUILD & VERIFY
   └─> cd ~/Documents/GitHub/email-admin-ui
   └─> CI=true npm run build
   └─> Check for errors/warnings (CI=true treats warnings as errors)

3. COMMIT & PUSH
   └─> git add -A && git commit -m "description" && git push

4. AUTOMATIC DEPLOYMENT (GitHub Actions)
   └─> Builds React app
   └─> Deploys to Firebase Hosting
   └─> Deploys Firebase Functions (functions/index.js)
   └─> NO manual firebase deploy needed!
```

### For Firebase Functions Changes (email-admin-ui/functions)

```
1. MAKE CHANGES
   └─> Edit functions/index.js

2. BUILD FRONTEND (includes functions)
   └─> cd ~/Documents/GitHub/email-admin-ui
   └─> CI=true npm run build

3. COMMIT & PUSH
   └─> git add -A && git commit -m "description" && git push

4. AUTOMATIC DEPLOYMENT
   └─> GitHub Actions deploys functions automatically
   └─> Check: https://console.firebase.google.com/project/email-automation-cache/functions
```

### For Backend Changes (EmailAutomation)

```
1. MAKE CHANGES
   └─> Edit files in email_automation/

2. VERIFY SYNTAX
   └─> python3 -m py_compile email_automation/<file>.py

3. RUN TESTS (MANDATORY - ALWAYS DO THIS)
   └─> python tests/standalone_test.py    # AI extraction tests (25 scenarios)
   └─> python tests/e2e_test.py           # Full pipeline E2E tests (5+ properties)
   └─> ALL TESTS MUST PASS before committing!

4. COMMIT & PUSH
   └─> git add -A && git commit -m "description" && git push

5. DEPLOYMENT
   └─> Render auto-deploys on push (if configured)
   └─> GitHub Actions runs email.yml every 30 mins
```

**⚠️ CRITICAL: Run BOTH test suites after ANY code change. The tests are the source of truth for production behavior.**

### CI/CD Summary

| Repo | On Push to Main | Manual Deploy Needed? |
|------|-----------------|----------------------|
| email-admin-ui | Builds React + deploys Hosting + deploys Functions | NO |
| EmailAutomation | Nothing (cron-based) | Render auto-deploys |

### Pre-Push Checklist

- [ ] `CI=true npm run build` passes (frontend)
- [ ] `python3 -m py_compile <file>` passes (backend)
- [ ] Tests pass (if applicable)
- [ ] Commit message is descriptive
- [ ] No secrets/credentials in code

---

## Testing Framework

### ⚠️ MANDATORY TESTING RULE

**ALWAYS run tests after ANY code change.** The test framework hits the SAME production code paths - the only difference is email sending is mocked. Tests are the source of truth for what will happen in production.

### Test Types

| Test Suite | Purpose | Command |
|------------|---------|---------|
| `standalone_test.py` | AI extraction tests (25 scenarios) | `python tests/standalone_test.py` |
| `e2e_test.py` | Full pipeline E2E tests (uses Scrub file) | `python tests/e2e_test.py` |
| `campaign_lifecycle_test.py` | Campaign lifecycle tests (11 scenarios) | `python tests/campaign_lifecycle_test.py` |
| `multi_turn_live_test.py` | Live email integration tests (3 scenarios) | `python tests/multi_turn_live_test.py` |
| `batch_runner.py` | Large-scale batch testing (279+ scenarios) | `python tests/batch_runner.py --suite tests/generated_suite/` |

### Running Tests

```bash
# Set API key (required)
export OPENAI_API_KEY='sk-...'

# ALWAYS run BOTH test suites:
python tests/standalone_test.py    # Must show: 25/25 PASS
python tests/e2e_test.py           # Must show: 5/5 PASS (or all available)

# Run specific scenarios:
python tests/standalone_test.py -s complete_info
python tests/e2e_test.py -p "699 Industrial"

# List available tests:
python tests/standalone_test.py -l
python tests/e2e_test.py --list
```

### Test Scenarios (25 total)

| Scenario | Tests |
|----------|-------|
| `complete_info` | All fields provided → closing email |
| `partial_info` | Some fields → request missing |
| `property_unavailable` | "No longer available" → event + ask alternatives |
| `unavailable_with_alternative` | Unavailable + new property suggested |
| `call_requested_with_phone` | Call request + phone → notification only |
| `call_requested_no_phone` | Call request, no phone → ask for number |
| `multi_turn_conversation` | Data accumulated across multiple messages |
| `vague_response` | No concrete data → re-request specifics |
| `new_property_suggestion` | Original available + new property mentioned |
| `close_conversation` | Natural conversation ending |
| `client_asks_requirements` | Broker asks about client's space requirements - AI escalates |
| `scheduling_request` | Broker offers tour - triggers tour_requested with suggested email |
| `negotiation_attempt` | Broker makes counteroffer - AI escalates |
| `identity_question` | Broker asks who the client is - AI escalates |
| `legal_contract_question` | Broker asks about contract/LOI - AI escalates |
| `mixed_info_and_question` | Broker provides info but also asks question requiring user input |
| `budget_question` | Broker asks about budget - AI escalates |
| `different_person_replies` | Different person signs email - Leasing Contact NOT updated |
| `new_property_suggestion_with_different_contact` | New property suggested - original contact NOT changed |
| `contact_optout_not_interested` | Broker says not interested → contact_optout event |
| `contact_optout_no_tenant_reps` | Broker refuses tenant reps → contact_optout:no_tenant_reps |
| `wrong_contact_redirected` | Wrong person, forwards to colleague → wrong_contact:forwarded |
| `wrong_contact_left_company` | Contact left company → wrong_contact:left_company |
| `property_issue_major` | Broker mentions significant property issue → property_issue:major |
| `property_issue_critical` | Health/safety concern → property_issue:critical |

### Campaign Lifecycle Tests

Tests the FULL campaign lifecycle from start to finish:
- Multiple properties going through various scenarios simultaneously
- Multi-turn conversations until resolution
- Sheet state changes (rows filled, moved below NON-VIABLE divider)
- Notification flow at each stage
- Campaign completion detection
- **Threading logic**: pause when escalated, resume after user input

```bash
# Run all campaign scenarios
python tests/campaign_lifecycle_test.py

# Run specific scenario
python tests/campaign_lifecycle_test.py -s mixed_outcomes

# List available scenarios
python tests/campaign_lifecycle_test.py -l
```

**Campaign Scenarios (6):**

| Scenario | Description | Expected Outcome |
|----------|-------------|------------------|
| `mixed_outcomes` | 5 properties: 2 complete, 1 unavailable, 1 needs input, 1 multi-turn | 3 complete, 1 non-viable, 1 needs action |
| `all_complete` | 3 properties all provide complete info | 3 complete, campaign done |
| `all_unavailable` | 3 properties all unavailable | 3 non-viable, campaign done |
| `new_properties_suggested` | Brokers suggest alternatives | Creates new property notifications |
| `escalation_scenarios` | Various user input required | 5 needs action |
| `multi_turn_completion` | Properties require 2+ turns | 2 complete after multi-turn |

**Threading Logic Scenarios (5):**

| Scenario | Description | What It Tests |
|----------|-------------|---------------|
| `pause_on_escalation` | 3 properties all trigger escalation | Verifies all pause in NEEDS_ACTION state |
| `resume_after_user_input` | Escalation → user input → broker reply | Tests full pause→resume→complete cycle |
| `pause_resume_complete_cycle` | 2 properties go through full cycle | Both complete after user provides input |
| `mixed_pause_and_complete` | 2 complete + 1 paused | Campaign NOT complete while 1 is paused |
| `close_conversation_terminates` | Broker closes conversation naturally | Terminates without needing user action |

### Batch Testing (Large Scale)

For comprehensive testing at scale (279+ test cases):

```bash
# Generate test suite (creates 279 test cases)
python tests/generate_test_suite.py --output tests/generated_suite/ --properties 15

# Run full batch with parallel execution
python tests/batch_runner.py --suite tests/generated_suite/ --parallel 4 --output tests/results/full_run/

# Analyze results and generate HTML report
python tests/analyze_results.py --results tests/results/full_run/ --export-html tests/results/full_run/report.html

# Compare runs for regression testing
python tests/analyze_results.py --results tests/results/run1/ --compare tests/results/run2/
```

**Latest Batch Test Results (2026-01-24):**
- Total Tests: 279
- Pass Rate: 100%
- P50 Latency: 2515ms
- P99 Latency: 7336ms

See `tests/TESTING_PLAN.md` for the comprehensive testing plan and methodology.

### Key Files

```
tests/
├── standalone_test.py           # AI extraction tests (25 scenarios)
├── e2e_test.py                  # Full pipeline E2E tests
├── campaign_lifecycle_test.py   # Campaign lifecycle tests (11 scenarios)
├── multi_turn_live_test.py      # Live email integration tests (3 scenarios)
├── multi_turn_scenarios.py      # Multi-turn scenario definitions
├── batch_runner.py              # Large-scale batch test execution
├── generate_test_suite.py     # Generate 279+ test cases
├── analyze_results.py         # Analyze results, generate reports
├── TESTING_PLAN.md            # Comprehensive testing plan
├── e2e_server.py              # HTTP server for frontend E2E tests
├── e2e_harness.py             # Test harness for processing
├── results_manager.py         # Results file management
├── conversation_generator.py  # Programmatic conversation generation
├── conversations/             # Broker reply fixtures (JSON)
│   ├── 699_industrial_park_dr.json
│   ├── 135_trade_center_court.json
│   ├── 2058_gordon_hwy.json
│   ├── 1_kuhlke_dr.json
│   ├── 1_randolph_ct.json
│   ├── edge_cases/            # Edge case scenarios
│   │   ├── hostile_response.json
│   │   ├── forward_to_colleague.json
│   │   └── ...
│   └── generated/             # Programmatically generated (90 scenarios)
│       ├── response_type/
│       ├── event/
│       ├── edge_case/
│       └── format/
└── results/                   # Saved test run outputs
    ├── run_YYYYMMDD_HHMMSS/   # E2E/batch results
    │   ├── manifest.json
    │   ├── summary.json
    │   └── {property}.json
    └── multi_turn_*.json      # Multi-turn live test results
```

### Test Data File

**`Scrub Augusta GA.xlsx`** (in project root) - Real-world property data for E2E testing. The `e2e_test.py` loads this file and processes it with the conversation files to simulate complete campaigns.

### E2E Test Architecture

The E2E tests simulate a **complete campaign**:
1. Load `Scrub Augusta GA.xlsx` → Gets real property data
2. Load `conversations/*.json` → Gets simulated broker replies
3. Call **ACTUAL production code** (`propose_sheet_updates`)
4. Verify outputs: sheet state, notifications, response emails

This ensures tests are a **1:1 reflection** of production behavior.

### Saving Results to Files

Run tests with `--save` to output structured JSON result files:

```bash
python3 tests/e2e_test.py --all --save
```

Results are saved to `tests/results/run_YYYYMMDD_HHMMSS/`:

| File | Contents |
|------|----------|
| `manifest.json` | Run metadata, input file hash, property list |
| `summary.json` | Pass/fail counts, coverage (events, columns, notifications) |
| `{property}.json` | Full result: input, conversation, output, sheet state, validation |

Each result file contains:
- **input**: Property data from Excel (row, columns, values)
- **conversation**: Message exchange used for testing
- **output**: AI response (updates, events, response email)
- **sheet_state**: Before/after column values
- **notifications**: Derived notifications
- **validation**: Expected vs actual comparison

### Comparing Runs

```bash
# List previous runs
python3 tests/e2e_test.py --list-runs

# Compare two runs to detect behavioral changes
python3 tests/e2e_test.py --compare run_20240115_100000 run_20240115_110000
```

### Generating Conversations Programmatically

Use `conversation_generator.py` to create conversations for all real-world scenarios:

```bash
# List available scenarios
python3 tests/conversation_generator.py --list-scenarios

# Generate all conversations (5 properties x 18 scenarios = 90 files)
python3 tests/conversation_generator.py --generate-all

# Run generated conversations
python3 tests/e2e_test.py --generated response_type --save
python3 tests/e2e_test.py --generated all --save
```

Generated scenario categories:
- **response_type**: complete_info, partial_info, vague_response, terse_response
- **event**: call_requested, property_unavailable, new_property, contact_optout
- **edge_case**: forward_to_colleague, out_of_office, flyer_link_only, tour_offer
- **format**: numbers_with_words, numbers_with_mixed_formats

### Adding New Conversation Tests

Create a JSON file in `tests/conversations/` matching the property address:

```json
{
  "property": "123 New Street",
  "city": "Augusta",
  "description": "What this tests",
  "messages": [
    {"direction": "outbound", "content": "Initial email sent..."},
    {"direction": "inbound", "content": "Broker reply..."}
  ],
  "expected_updates": [
    {"column": "Total SF", "value": "10000"}
  ],
  "expected_events": [],
  "forbidden_updates": ["Leasing Contact", "Email"]
}
```

### Adding/Modifying Scenarios

Edit `tests/standalone_test.py` - the `SCENARIOS` list contains all test cases:

```python
TestScenario(
    name="scenario_name",
    description="What this tests",
    property_address="1 Randolph Ct",  # Must match PROPERTIES dict
    messages=[
        {"direction": "outbound", "content": "..."},
        {"direction": "inbound", "content": "..."},
    ],
    expected_updates=[
        {"column": "Total SF", "value": "15000"},
    ],
    expected_events=["property_unavailable"],  # or []
    expected_response_type="missing_fields"  # or "closing", "unavailable", etc.
)
```

### Multi-Turn Live Email Tests

End-to-end tests that send **real emails** between Outlook (Microsoft Graph) and Gmail (SMTP), running through the actual production pipeline (`main.py`) each turn. Tests thread matching, AI extraction, escalation flow, and response quality with real email delivery.

```bash
# Run all 3 scenarios
python tests/multi_turn_live_test.py

# Run specific scenario
python tests/multi_turn_live_test.py --scenario gradual_info_gathering

# Resume interrupted run
python tests/multi_turn_live_test.py --resume

# Custom wait time for email delivery (default: 75s)
python tests/multi_turn_live_test.py --wait 90

# List available scenarios
python tests/multi_turn_live_test.py --list

# Clean up test data from Firestore
python tests/multi_turn_live_test.py --cleanup
```

**Scenarios:**

| Scenario | Turns | Description |
|----------|-------|-------------|
| `gradual_info_gathering` | 4 | Broker provides info across 3 replies, AI gathers until complete |
| `escalation_and_resume` | 4 | Broker asks identity → AI escalates → user replies via frontend → broker completes |
| `mixed_info_and_question` | 4 | Broker provides data AND asks question → AI extracts AND escalates |

**Per-turn verification:**
- Thread message count in Firestore
- Sheet field extraction accuracy (expected vs actual values)
- Notification kinds (sheet_update, action_needed, row_completed)
- Escalation reason matching (e.g., `needs_user_input:confidential`)
- No duplicate emails sent
- Listing Brokers Comments quality (contextual notes, no redundant column data)

**Escalation flow tested:**
1. AI detects `needs_user_input` → creates `action_needed` notification → does NOT auto-reply
2. Test creates outbox entry (simulating frontend "Send Email" button)
3. Pipeline sends via Graph API → `scan_sent_items_for_manual_replies` indexes it
4. Next broker reply processed with full conversation history

### What Tests Validate

1. **Field extraction** - Correct columns and values parsed
2. **Forbidden fields** - Never writes "Gross Rent" (formula), never requests "Rent/SF /Yr" or "Gross Rent"
3. **Event detection** - All 9 event types: `property_unavailable`, `new_property`, `call_requested`, `close_conversation`, `tour_requested`, `needs_user_input`, `contact_optout`, `wrong_contact`, `property_issue`
4. **Response quality** - Professional, concise, doesn't request forbidden fields
5. **Number formatting** - Plain decimals, no "$" or "SF" symbols
6. **Read-only field protection** - Leasing Contact, Email never overwritten even when different person replies
7. **Escalation subreasons** - Correct subreason detected (e.g., `needs_user_input:confidential` vs `needs_user_input:client_question`)

### Workflow for Changes

1. **Make code changes** to `email_automation/ai_processing.py` or `processing.py`
2. **Run tests**: `python tests/standalone_test.py`
3. **If tests fail**: Fix issues, re-run
4. **If tests pass**: Changes are production-ready

### Sheet Column Reference (for tests)

```
Property Address, City, Property Name, Leasing Company, Leasing Contact,
Email, Total SF, Rent/SF /Yr, Ops Ex /SF, Gross Rent, Drive Ins, Docks,
Ceiling Ht, Power, Listing Brokers Comments, Flyer / Link, Floorplan,
Jill and Clients comments
```

### Required Fields (for "all complete" detection)

```python
["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
# NEVER request: "Rent/SF /Yr" (provided voluntarily, not requested)
# NEVER write: "Gross Rent" (formula column: =(H+I)*G/12, auto-calculates monthly rent)
```

---

## Full E2E Integration Testing

### Overview

The E2E testing setup allows running the complete frontend + backend flow with:
- Firebase Emulators (Auth, Firestore, Functions, Storage)
- Python backend server (real AI processing)
- React frontend in test mode
- Playwright for UI automation

### Quick Start

```bash
# Terminal 1: Start all services
cd ~/Documents/GitHub/email-admin-ui
./e2e/run-e2e.sh manual

# This starts:
# - Firebase Emulators on ports 8080 (Firestore), 9099 (Auth), etc.
# - Python E2E Server on port 5002
# - React dev server on port 3000

# Test credentials (emulator mode):
# Email: test@example.com
# Password: testpassword123
```

### Running E2E Tests

```bash
# Run all E2E tests
./e2e/run-e2e.sh test

# Run only modal interaction tests
./e2e/run-e2e.sh test-modal

# Run full journey tests
./e2e/run-e2e.sh test-journey

# Run with specific filter
./e2e/run-e2e.sh test --filter "new_property"
```

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     PLAYWRIGHT TESTS                        │
│  (e2e/tests/*.spec.js)                                      │
└───────────────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   FRONTEND   │  │   BACKEND    │  │   FIREBASE   │
│   (React)    │  │   (Python)   │  │  EMULATORS   │
│ localhost:   │  │ localhost:   │  │              │
│   3000       │  │   5002       │  │ Auth: 9099   │
└──────┬───────┘  └──────┬───────┘  │ FS: 8080     │
       │                 │          │ Fn: 5001     │
       │   Firestore     │          └──────┬───────┘
       └─────────────────┴─────────────────┘
```

### Test Files

| File | Purpose |
|------|---------|
| `e2e/run-e2e.sh` | Orchestration script to start/stop all services |
| `e2e/backend-simulator.js` | Mock or real backend for testing |
| `e2e/test-utils.js` | Test utilities (seed data, clear data) |
| `e2e/tests/modal-interactions.spec.js` | Modal UI interaction tests |
| `e2e/tests/full-journey.spec.js` | Complete workflow tests |
| `tests/e2e_server.py` | Python HTTP server for real backend |
| `tests/e2e_harness.py` | Backend test harness with mocked Graph API |

### Test Scenarios

The modal interaction tests cover:

1. **New Property Suggestion** (`new_property_pending_approval`)
   - Notification appears in sidebar
   - Modal opens with property context
   - Contact name personalization works
   - Referral context shown (e.g., "Marcus mentioned...")
   - Send email creates outbox entry

2. **Needs User Input** (`needs_user_input:*`)
   - Identity questions (`needs_user_input:confidential`)
   - Requirements questions (`needs_user_input:client_question`)
   - Negotiation requests (`needs_user_input:negotiation`)
   - Chatbot shows proactive message asking for user input

3. **Tour Requested** (`tour_requested`)
   - Tour offer shown in context
   - Suggested response email pre-filled
   - User can modify via AI chat

### Manual Testing Workflow

1. **Start services**: `./e2e/run-e2e.sh manual`
2. **Open browser**: http://localhost:3000
3. **Login** with test credentials
4. **Create a client** using the Scrub file
5. **Launch a campaign** (sends emails via backend)
6. **Trigger backend processing**: The Python server processes outbox
7. **Simulate broker reply**: Use the backend API to inject a reply
8. **See notification**: Frontend picks up notification in real-time
9. **Interact with modal**: Click notification, compose response, send

### Using Real Backend

To use the real Python backend with actual OpenAI processing:

```bash
# Set your OpenAI API key
export OPENAI_API_KEY='sk-...'

# Start with real backend
./e2e/run-e2e.sh manual

# Or run tests with real backend
USE_REAL_BACKEND=true ./e2e/run-e2e.sh test
```

### Injecting Test Data

From the tests or manually:

```javascript
// Create a notification directly
await db.collection('users').doc('test-user-123')
  .collection('clients').doc(clientId)
  .collection('notifications').add({
    kind: 'action_needed',
    priority: 'important',
    meta: {
      reason: 'needs_user_input:confidential',
      question: 'What company is this for?',
    },
    createdAt: new Date(),
  });

// Or via Python backend API:
// POST http://localhost:5002/inject-reply
// { "userId": "test-user-123", "fromEmail": "broker@test.com", "body": "..." }
```

### Campaign Simulation API

The E2E server provides endpoints for simulating broker responses:

```bash
# Simulate a broker response with AI processing
curl -X POST http://localhost:5002/api/simulate-response \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "test-client",
    "property": {
      "address": "100 Industrial Way",
      "city": "Augusta",
      "contact": "John Smith",
      "email": "john@broker.com",
      "rowIndex": 3
    },
    "responseType": "complete_info"
  }'

# Response types available:
# - complete_info        : All fields → row complete, closing email
# - partial_info         : Some fields → follow-up request
# - complete_remaining   : Second turn completing partial
# - property_unavailable : Moved to non-viable
# - new_property_different_contact : New property + contact
# - call_requested       : Escalates to user
# - tour_offered         : Tour notification
# - identity_question    : needs_user_input:confidential
# - budget_question      : needs_user_input:client_question
# - negotiation_attempt  : needs_user_input:negotiation

# Get campaign state
curl http://localhost:5002/api/campaign-state

# Get all notifications
curl http://localhost:5002/api/notifications

# Reset test state
curl -X POST http://localhost:5002/api/reset
```

### Comprehensive Testing Workflow

For rigorous campaign lifecycle testing:

1. **Backend unit tests** (no frontend needed):
   ```bash
   python tests/standalone_test.py          # 25 AI scenarios
   python tests/campaign_lifecycle_test.py  # 11 campaign lifecycle scenarios
   ```

2. **Full integration tests** (frontend + backend):
   ```bash
   cd ~/Documents/GitHub/email-admin-ui
   ./e2e/run-e2e.sh test
   ```

3. **Manual exploration**:
   ```bash
   ./e2e/run-e2e.sh manual
   # Then manually:
   # - Create client with Scrub file
   # - Launch campaign
   # - Use /api/simulate-response to inject broker replies
   # - Watch notifications appear in real-time
   # - Interact with modals
   # - Verify sheet updates
   ```

### Expected Campaign Outcomes

| Scenario | Sheet State | Notifications |
|----------|-------------|---------------|
| 3 complete_info | 3 rows filled, all fields | 3 row_completed |
| 2 complete + 1 unavailable | 2 filled, 1 below NON-VIABLE | 2 row_completed + 1 property_unavailable |
| 1 partial + 1 complete | 1 in progress, 1 filled | Multiple sheet_update + 1 row_completed |
| 1 needs_user_input | Unchanged | 1 action_needed |
| Campaign complete | All rows resolved (complete OR non-viable) | Campaign complete indicator |

---

## Frontend Integration (email-admin-ui)

### Key Frontend Components

| Component | Purpose |
|-----------|---------|
| `ClientsPage.jsx` | Client management, upload Excel, create Google Sheets |
| `Dashboard.jsx` | Main dashboard with stats and client table |
| `ClientsTable.jsx` | Lists clients with actions (Get Started, View) |
| `StartProjectModal.jsx` | Launch outreach campaigns, personalize emails |
| `NewPropertyRequestModal.jsx` | Handle new property suggestions from backend |
| `NotificationsSidebar.jsx` | Real-time notification display |
| `useNotifications.js` | Hook for listening to backend notifications |

### Frontend → Backend Data Flow

```
1. User uploads Excel (AddClientModal)
   └─> Firebase Function `analyzeSheetColumns` (AI maps columns)
   └─> Firebase Function `api` (creates Google Sheet)
   └─> Frontend saves to `users/{uid}/clients/{clientId}`

2. User launches campaign (StartProjectModal)
   └─> Frontend creates outbox entries per property/broker
   └─> Backend reads outbox every 30 min, sends emails, deletes entries

3. Backend detects reply
   └─> Writes to `threads/`, `msgIndex/`, `convIndex/`
   └─> Extracts data via OpenAI
   └─> Writes to Google Sheets
   └─> Creates `notifications/` for frontend
```

### Outbox Document Structure (Frontend writes, Backend consumes)

```javascript
{
  id: string,                    // Firestore doc ID
  clientId: string,              // Reference to client
  assignedEmails: string[],      // Recipients (single email if personalized)
  script: string,                // Email body
  secondaryScript: string|null,  // Follow-up script
  subject: string,
  contactName: string,           // Full contact name
  firstName: string,             // For [NAME] personalization
  property: {
    address: string,
    city: string,
    propertyName: string,
    rowIndex: number             // Sheet row to track
  },
  isPersonalized: boolean,       // True if [NAME] was replaced
  createdAt: Timestamp           // For send ordering
}
```

### Notification Structure (Backend writes, Frontend reads)

```javascript
{
  id: string,
  kind: "sheet_update" | "action_needed" | "row_completed" | "property_unavailable" | "conversation_closed",
  createdAt: Timestamp,
  priority: "important" | "normal",

  // For sheet_update:
  meta: { column: string, address: string, oldValue: any, newValue: any, reason: string, confidence: string },

  // For action_needed:
  meta: {
    reason: string,  // See reason values below
    address: string,
    city: string,
    link: string,
    notes: string,
    status: "pending_approval" | "pending_send",
    suggestedEmail: { to: string[], subject: string, body: string }  // For tour_requested
  },

  // For row_completed:
  rowAnchor: string,

  // For conversation_closed:
  meta: { reason: "natural_end", details: string, lastMessage: string }
}
```

**`action_needed` reason values:**

| Reason | Trigger |
|--------|---------|
| `call_requested` | Broker explicitly asks for a phone call |
| `tour_requested` | Broker offers property tour/showing |
| `new_property_pending_approval` | Broker suggests a different property |
| `missing_fields` | Required fields still missing after follow-up |
| `needs_user_input:confidential` | Broker asks who the client is |
| `needs_user_input:client_question` | Broker asks about requirements/budget |
| `needs_user_input:scheduling` | Tour/meeting scheduling request |
| `needs_user_input:negotiation` | Price or term negotiation |
| `needs_user_input:legal_contract` | Contract/LOI/lease questions |
| `needs_user_input:unclear` | Ambiguous message (fallback) |
| `contact_optout:not_interested` | General disinterest |
| `contact_optout:unsubscribe` | Explicit removal request |
| `contact_optout:do_not_contact` | Firm request to stop contact |
| `contact_optout:no_tenant_reps` | Policy against tenant reps |
| `contact_optout:direct_only` | Only deals directly with tenants |
| `contact_optout:hostile` | Rude/aggressive response |
| `wrong_contact:no_longer_handles` | Used to handle but doesn't anymore |
| `wrong_contact:wrong_person` | Never handled this property |
| `wrong_contact:forwarded` | Forwarding to correct person |
| `wrong_contact:left_company` | No longer with the company |
| `property_issue:critical` | Health/safety concern |
| `property_issue:major` | Significant repair needed |
| `property_issue:minor` | Cosmetic/inconvenience |
```

### Event Types (AI-detected → Backend → Frontend)

| Event | Trigger | Backend Action | Frontend Action |
|-------|---------|----------------|-----------------|
| `property_unavailable` | Broker says not available | Moves row below NON-VIABLE | Shows property_unavailable notification |
| `new_property` | Broker suggests different property | Creates pending approval entry | Shows approval modal with suggested email |
| `call_requested` | Broker wants to talk | Creates action_needed notification | Shows action button |
| `tour_requested` | Broker offers tour/showing | Creates notification + suggested email | Shows pre-filled response for approval |
| `close_conversation` | Natural end of thread | Creates conversation_closed notification | Stops processing thread |
| `needs_user_input` | Question AI can't answer | Creates action_needed + pauses thread | Shows chatbot for user to compose reply |
| `contact_optout` | Contact refuses communication | Adds to opt-out list | Shows action_needed notification |
| `wrong_contact` | Wrong person for property | Creates action_needed notification | Shows redirect info |
| `property_issue` | Broker mentions property problem | Creates action_needed notification | Shows issue details by severity |

**Notification kinds (not AI events):**

| Kind | Trigger | Frontend Action |
|------|---------|-----------------|
| `sheet_update` | AI extracts a field value | Shows in notification sidebar |
| `row_completed` | All required fields filled | Marks property complete |

### Firebase Cloud Functions (in email-admin-ui/functions)

| Function | Purpose |
|----------|---------|
| `api` | Creates Google Sheet from uploaded Excel |
| `deleteSheet` | Deletes Google Sheet when client removed |
| `analyzeSheetColumns` | AI maps Excel columns to canonical fields |
| `generateAllScripts` | Batch generates follow-up emails |
| `generateSecondaryScript` | Regenerates single follow-up |
| `chatWithPropertyContext` | AI chat for email composition |

---

## AI Processing Rules

### Forbidden Actions
- **Never write** `Gross Rent` (formula column)
- **Never request** `Rent/SF /Yr` or `Gross Rent` from brokers
- **Never reveal** client identity or budget
- **Never commit** to tours, contracts, or negotiations
- **Never answer** questions requiring user input (forward to user instead)

### Read-Only Fields (AI should NEVER update)
These fields contain pre-existing client data that should NEVER be changed by AI:
- `Property Address`
- `City`
- `Property Name`
- `Leasing Company`
- `Leasing Contact` ← Even if someone else signs the email!
- `Email`

The AI may ONLY update extractable property specs (Total SF, Ops Ex, Drive Ins, Docks, Ceiling Ht, Power, etc.)

### Response Types
| Type | When | Action |
|------|------|--------|
| `closing` | All required fields complete | Send thank-you, close thread |
| `missing_fields` | Some fields still needed | Request missing info |
| `unavailable` | Property not available | Ask for alternatives |
| `new_property` | Broker suggests new property | Create notification for approval |
| `call_requested` | Broker wants to call | Create action_needed notification |
| `forward_to_user` | Question AI can't answer | Create notification for user |

---

## Sheet Operations

### NON-VIABLE Divider
- Row in sheet separating viable from non-viable properties
- Properties marked unavailable are moved BELOW this divider
- New properties are inserted ABOVE this divider
- `sheet_operations.py` handles row movement

### Column Mapping (`column_config.py`)

Dynamic column mapping system that translates between canonical field names and actual sheet column headers:

**Canonical Fields:**
- `property_address`, `city`, `property_name`, `leasing_company`, `leasing_contact`, `email` (read-only, matching)
- `total_sf`, `rent_sf_yr`, `ops_ex_sf`, `gross_rent`, `drive_ins`, `docks`, `ceiling_ht`, `power` (extractable)
- `listing_comments` (append-only with "•" separator, contextual notes only)
- `flyer_link`, `floorplan` (append-only, extractable)
- `client_comments` (read-only)

**Key functions:**
- `detect_column_mapping(headers)` - Maps sheet columns to canonical fields (exact match then AI semantic match)
- `build_column_rules_prompt(column_config)` - Generates AI extraction rules from config
- `get_required_fields_for_close(column_config)` - Returns fields needed for conversation completion

**Required for close:** `Total SF`, `Ops Ex /SF`, `Drive Ins`, `Docks`, `Ceiling Ht`, `Power`
**Never request:** `Rent/SF /Yr` (accepted if volunteered, never asked for)
**Never write:** `Gross Rent` (formula column: `=(H+I)*G/12`)

### Follow-Up System (`followup.py`)

Automatic follow-up emails for non-responsive brokers:
- 0-3 configurable follow-ups per thread with custom wait times
- Sends as replies to maintain thread continuity
- **Pauses** when broker responds; **resumes** if broker goes silent again
- Default escalation: friendly reminder → gentle nudge → final attempt
- Called from `main.py` every 30 minutes via `check_and_send_followups()`

### Listing Brokers Comments

The "Listing Brokers Comments" column stores contextual notes about properties:
- Written via AI's `notes` field using `_append_notes_to_comments()`
- Uses "•" bullet separator, append mode (never overwrites)
- Should contain contextual info: NNN, lease terms, building features, zoning, condition
- Should NOT contain redundant column data (SF, rent, docks, ceiling, power values)
