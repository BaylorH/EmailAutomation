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
