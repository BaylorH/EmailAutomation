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

Hosted on Render.com (`https://email-token-manager.onrender.com`) with GitHub Actions keep-alive pings every 14 minutes to prevent free tier sleeping.

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

3. RUN TESTS (if AI/processing changes)
   └─> python tests/standalone_test.py

4. COMMIT & PUSH
   └─> git add -A && git commit -m "description" && git push

5. DEPLOYMENT
   └─> Render auto-deploys on push (if configured)
   └─> GitHub Actions runs email.yml every 30 mins
```

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

### Overview

The `tests/` directory contains a comprehensive test suite that validates the AI extraction and conversation handling logic WITHOUT needing Firebase, Google Sheets, or actual emails. It calls OpenAI directly with simulated conversations.

### Running Tests

```bash
# Set API key (required)
export OPENAI_API_KEY='sk-...'

# Run all 10 scenarios
python tests/standalone_test.py

# Run specific scenario
python tests/standalone_test.py -s complete_info

# List all scenarios
python tests/standalone_test.py -l

# Save results to JSON
python tests/standalone_test.py -r results.json
```

### Test Scenarios (10 total)

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

### Key Files

```
tests/
├── standalone_test.py    # Main test runner (self-contained, only needs OpenAI)
├── mock_data.py          # Sheet structure + all scenario definitions
├── test_results.json     # Last test run results
└── TEST_SCENARIOS.md     # Detailed scenario documentation
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
# NEVER write: "Gross Rent" (formula column: =H+I+G/12, auto-calculates)
```
