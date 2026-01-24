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
  main.py           → Runs processing for all users
  app.py            → Flask server for OAuth + APIs
  scheduler_runner.py → Alternative extended scheduler

Core Pipeline (email_automation/):
  1. email.py           → Outbox processing, send drafts
  2. processing.py      → Inbox scanning, thread matching (main logic)
  3. ai_processing.py   → OpenAI extraction, field validation
  4. sheets.py          → Google Sheets updates
  5. messaging.py       → Firestore thread/message storage

Support Modules:
  clients.py          → Firestore & Google API client init
  email_operations.py → Specialized email sending (replies, templates)
  sheet_operations.py → Advanced sheet manipulation
  utils.py            → Retry logic, HTML parsing, encoding helpers
  notifications.py    → User notifications
  file_handling.py    → PDF attachment handling
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
- `notifications/` - Per-client notifications (sheet_update, action_needed, row_completed)

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

- `/auth/login` - Initiate MSAL OAuth flow (stores token in Firebase Storage)
- `/auth/callback` - OAuth completion
- `/api/status` - Token status check
- `/api/trigger-scheduler` - Manually run processing
- `/api/debug-inbox` - Debug incoming emails
- `/api/debug-thread-matching` - Debug conversation matching

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
   └─> python tests/standalone_test.py    # AI extraction tests (19 scenarios)
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
| `standalone_test.py` | AI extraction tests (19 scenarios) | `python tests/standalone_test.py` |
| `e2e_test.py` | Full pipeline E2E tests (uses Scrub file) | `python tests/e2e_test.py` |

### Running Tests

```bash
# Set API key (required)
export OPENAI_API_KEY='sk-...'

# ALWAYS run BOTH test suites:
python tests/standalone_test.py    # Must show: 19/19 PASS
python tests/e2e_test.py           # Must show: 5/5 PASS (or all available)

# Run specific scenarios:
python tests/standalone_test.py -s complete_info
python tests/e2e_test.py -p "699 Industrial"

# List available tests:
python tests/standalone_test.py -l
python tests/e2e_test.py --list
```

### Test Scenarios (19 total)

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
| `scheduling_request` | Broker requests tour scheduling - AI escalates |
| `negotiation_attempt` | Broker makes counteroffer - AI escalates |
| `identity_question` | Broker asks who the client is - AI escalates |
| `legal_contract_question` | Broker asks about contract/LOI - AI escalates |
| `mixed_info_and_question` | Broker provides info but also asks question requiring user input |
| `budget_question` | Broker asks about budget - AI escalates |
| `different_person_replies` | Different person signs email - Leasing Contact NOT updated |
| `new_property_suggestion_with_different_contact` | New property suggested - original contact NOT changed |

### Key Files

```
tests/
├── standalone_test.py       # AI extraction tests (19 scenarios)
├── e2e_test.py              # Full pipeline E2E tests
├── results_manager.py       # Results file management
├── conversation_generator.py # Programmatic conversation generation
├── conversations/           # Broker reply fixtures (JSON)
│   ├── 699_industrial_park_dr.json
│   ├── 135_trade_center_court.json
│   ├── 2058_gordon_hwy.json
│   ├── 1_kuhlke_dr.json
│   ├── 1_randolph_ct.json
│   ├── edge_cases/          # Edge case scenarios
│   │   ├── hostile_response.json
│   │   ├── forward_to_colleague.json
│   │   └── ...
│   └── generated/           # Programmatically generated (90 scenarios)
│       ├── response_type/
│       ├── event/
│       ├── edge_case/
│       └── format/
└── results/                 # Saved test run outputs
    └── run_YYYYMMDD_HHMMSS/
        ├── manifest.json    # Run metadata + input file hash
        ├── summary.json     # Campaign-level results
        └── {property}.json  # Per-property results
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

### What Tests Validate

1. **Field extraction** - Correct columns and values parsed
2. **Forbidden fields** - Never writes "Gross Rent" (formula), never requests "Rent/SF /Yr" or "Gross Rent"
3. **Event detection** - `property_unavailable`, `new_property`, `call_requested`, `close_conversation`
4. **Response quality** - Professional, concise, doesn't request forbidden fields
5. **Number formatting** - Plain decimals, no "$" or "SF" symbols

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
  kind: "sheet_update" | "action_needed" | "row_completed" | "property_unavailable",
  createdAt: Timestamp,
  priority: "important" | "normal",

  // For sheet_update:
  meta: { column: string, address: string, newValue: any },

  // For action_needed:
  meta: {
    reason: "new_property_pending_approval" | "call_requested" | "missing_fields",
    address: string,
    city: string,
    link: string,
    notes: string,
    status: "pending_approval" | "pending_send",
    suggestedEmail: { to: string[], subject: string, body: string }
  },

  // For row_completed:
  rowAnchor: string
}
```

### Event Types (Backend → Frontend)

| Event | Trigger | Frontend Action |
|-------|---------|-----------------|
| `sheet_update` | AI extracts a field value | Shows in notification sidebar |
| `row_completed` | All required fields filled | Marks property complete |
| `action_needed` | Call requested, new property suggested | Shows action button |
| `property_unavailable` | Broker says not available | Moves row below NON-VIABLE |
| `new_property` | Broker suggests new property | Creates pending approval notification |
| `call_requested` | Broker wants to talk | Creates action_needed notification |
| `close_conversation` | Natural end of thread | Stops processing thread |

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

### Column Mapping
- `column_config.py` defines canonical field names
- AI maps broker responses to canonical fields
- Case-insensitive matching with normalization
