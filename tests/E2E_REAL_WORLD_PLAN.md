# E2E Real World Test Plan

**Date:** March 2026
**Test File:** `test_pdfs/E2E_Real_World_Test.xlsx`
**Follow-up Config:** 3 follow-ups at **1 hour, 2 hours, 3 hours**

---

## Test Structure

### Part 1: Follow-Up Verification (3+ hours)
- Start campaign
- **Do NOT reply to any emails**
- Wait for all 3 follow-ups to send on all 7 properties
- Verify follow-ups went to BROKERS (not to self) - **BUG FIX TEST**

### Part 2: Complete Campaign
- Send broker replies for all 7 properties
- Test PDF extraction, escalations, Parking Spaces
- Verify all scenarios work correctly

---

## Properties Overview

| Row | Property | Contact | Email | Phase 2 Scenario |
|-----|----------|---------|-------|------------------|
| 3 | 699 Industrial Park Dr | Jeff Wilson | bp21harrison | Complete + PDF |
| 4 | 135 Trade Center Court | Luke Coffey | bp21harrison | Unavailable + New Property |
| 5 | 2017 St. Josephs Drive | Brian Greene | manifold | Tour Request |
| 6 | 9300 Lottsford Rd | Craig Cheney | manifold | Complete + Parking |
| 7 | 1 Randolph Ct | Scott Atkins | bp21harrison | Identity Question |
| 8 | 1800 Broad St | Marcus Thompson | bp21harrison | Complete + PDF + Parking |
| 9 | 2525 Center West Pkwy | Lisa Anderson | manifold | Partial → Ask Parking → Complete |

---

## Pre-Test Checklist

- [ ] Clear `bp21harrison@gmail.com` inbox
- [ ] Clear `baylor@manifoldengineering.ai` inbox
- [ ] Firebase cleaned (Claude did this)
- [ ] Outlook cleaned (Claude did this)

---

# PART 1: FOLLOW-UP VERIFICATION

## Phase 1.1: Campaign Setup

### User Actions
1. Go to dashboard, click "New Campaign"
2. Upload `test_pdfs/E2E_Real_World_Test.xlsx`
3. Map columns:
   - Standard fields (Total SF, Docks, etc.) → "Ask"
   - **Parking Spaces → "Ask"** (NEW FIELD TEST)
4. Configure follow-ups:
   - **3 follow-ups**
   - **1 hour, 2 hours, 3 hours**
5. Start campaign
6. **Tell Claude "campaign started"**

---

## Phase 1.2: Initial Send

### Claude Actions
```bash
python main.py 2>&1 | tee /tmp/e2e_initial_send.log
```

### Expected
- 7 emails sent (grouped by broker)
- bp21harrison@gmail.com: 4 emails (rows 3, 4, 7, 8)
- baylor@manifoldengineering.ai: 3 emails (rows 5, 6, 9)
- All rows highlighted yellow
- Follow-up #1 scheduled for T+1 hour

### Verify
```bash
# Check threads created
python tests/e2e_helpers.py status
```

---

## Phase 1.3: Wait for Follow-ups

### Timeline

| Time | Event | Claude Action |
|------|-------|---------------|
| T+0 | Initial emails sent | Log and verify |
| T+1h | Follow-up #1 due | Run `main.py`, verify sent |
| T+2h | Follow-up #2 due | Run `main.py`, verify sent |
| T+3h | Follow-up #3 due | Run `main.py`, verify sent |

**User Action:** Check back in ~3 hours and tell Claude "check follow-ups"

### Claude Verification (at T+3h)
```bash
# Check Outlook SentItems - follow-ups should go to brokers, NOT self
python3 << 'EOF'
from tests.outlook_helper import get_access_token
import requests

token = get_access_token("NO7lVYVp6BaplKYEfMlWCgBnpdh2")
headers = {"Authorization": f"Bearer {token}"}

resp = requests.get(
    "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages",
    headers=headers,
    params={"$top": "50", "$select": "subject,sentDateTime,toRecipients", "$orderby": "sentDateTime desc"}
)

print("📤 SENT ITEMS (checking follow-up recipients):\n")
for msg in resp.json().get("value", []):
    to_list = msg.get("toRecipients", [])
    to = to_list[0]["emailAddress"]["address"] if to_list else "unknown"
    subj = msg.get("subject", "")[:40]
    print(f"  To: {to:<35} | {subj}")
EOF
```

### Success Criteria for Part 1
- [ ] 7 initial emails sent to correct brokers
- [ ] 7 × 3 = **21 follow-up emails** sent total
- [ ] All follow-ups sent to `bp21harrison@gmail.com` or `baylor@manifoldengineering.ai`
- [ ] **ZERO follow-ups sent to `baylor.freelance@outlook.com`** (bug fix verified!)
- [ ] All threads show `followUpStatus: max_reached`

---

# PART 2: COMPLETE CAMPAIGN

## Phase 2.1: Send All Broker Replies

**After verifying follow-ups work, send these replies:**

---

### Reply 1: 699 Industrial Park Dr → COMPLETE INFO + PDF
**From:** bp21harrison@gmail.com
**Reply to:** Most recent follow-up for this property
**Attach:** `test_pdfs/pdfs/699 Industrial Park Drive - Property Flyer.pdf`

```
Hi Jill,

Thanks for following up! Here are the details on 699 Industrial Park Dr:

- Total SF: 45,000
- Rate: $5.25/SF NNN
- Operating expenses: $1.85/SF
- 4 dock doors, 2 drive-ins
- 28' clear height
- 1200 amps, 3-phase
- 75 parking spaces

See attached flyer.

Jeff Wilson
```

**Expected:** All fields extracted (including parking), closing email, `row_completed`

---

### Reply 2: 135 Trade Center Court → UNAVAILABLE + NEW PROPERTY
**From:** bp21harrison@gmail.com
**Attach:** `test_pdfs/real_world/135 Trade Center Court - Brochure.pdf`

```
Hi Jill,

Sorry for the delay - 135 Trade Center Court just went under contract last week.

However, I have another property at Gun Club Industrial Park - 150 Trade Center Court. It's 7,500 SF at $15/SF NNN with ample parking. Attached is the brochure.

Let me know if interested.

Luke Coffey
```

**Expected:**
- `property_unavailable` → Row moves to NON-VIABLE
- `action_needed` notification for new property approval
- AI extracts from PDF: 7,500 SF, $15/SF NNN

---

### Reply 3: 2017 St. Josephs Drive → TOUR REQUEST
**From:** baylor@manifoldengineering.ai

```
Hi Jill,

Thanks for your persistence! I'd love to show you 2017 St. Josephs Drive.

Are you available Thursday or Friday afternoon for a tour?

Brian Greene
```

**Expected:** `tour_requested`, thread paused, `action_needed` notification

---

### Reply 4: 9300 Lottsford Rd → COMPLETE INFO + PARKING
**From:** baylor@manifoldengineering.ai

```
Hi Jill,

Here are the details on 9300 Lottsford:

- 28,000 SF
- $6.50/SF NNN
- OpEx: $2.25/SF
- 3 docks, 1 drive-in
- 24' clear
- 800 amps
- 45 parking spaces

Craig Cheney
```

**Expected:** All fields extracted (including parking), closing email, `row_completed`

---

### Reply 5: 1 Randolph Ct → IDENTITY QUESTION
**From:** bp21harrison@gmail.com

```
Hi Jill,

Before I share details on 1 Randolph Ct, I need to know who your client is. Our ownership requires this.

Who are you representing?

Scott Atkins
```

**Expected:** `needs_user_input:confidential`, thread paused, `action_needed`

---

### Reply 6: 1800 Broad St → COMPLETE + PARKING (NEW FIELD TEST)
**From:** bp21harrison@gmail.com
**Attach:** `test_pdfs/pdfs/1800 Broad Street - Property Flyer.pdf`

```
Hi Jill,

Here's everything on 1800 Broad St:

- 52,000 SF total
- $4.75/SF NNN
- OpEx: $1.50/SF
- 6 dock doors, 2 drive-ins
- 32' clear
- 2000 amps
- Parking: 95 spaces

Flyer attached.

Marcus Thompson
```

**Expected:** All fields extracted INCLUDING **Parking = 95**, closing email, `row_completed`

---

### Reply 7: 2525 Center West Pkwy → PARTIAL (Missing Parking)
**From:** baylor@manifoldengineering.ai

```
Hi Jill,

Here's what I have on 2525 Center West:

- 35,000 SF
- $5.00/SF NNN
- $1.75 OpEx
- 4 docks, 2 drive-ins
- 26' clear
- 1000 amps

Let me know if you need anything else.

Lisa Anderson
```

**Expected:**
- Partial extraction (no parking)
- AI auto-reply **asks for parking spaces** (testing new field!)
- Thread stays `active`

---

## Phase 2.2: Process Replies

### Claude Actions
```bash
python main.py 2>&1 | tee /tmp/e2e_replies.log
```

### Expected Results After Processing

| Row | Property | Status | Parking Extracted? | Notification |
|-----|----------|--------|-------------------|--------------|
| 3 | 699 Industrial Park | `completed` | ✅ 75 | `row_completed` |
| 4 | 135 Trade Center | NON-VIABLE | N/A | `property_unavailable`, `action_needed` |
| 5 | 2017 St. Josephs | `paused` | N/A | `action_needed` (tour) |
| 6 | 9300 Lottsford | `completed` | ✅ 45 | `row_completed` |
| 7 | 1 Randolph Ct | `paused` | N/A | `action_needed` (identity) |
| 8 | 1800 Broad St | `completed` | ✅ 95 | `row_completed` |
| 9 | 2525 Center West | `active` | ❌ (AI asking) | `sheet_update` |

---

## Phase 2.3: Complete Multi-Turn (2525 Center West)

### Reply 8: 2525 Center West → ADD PARKING
**From:** baylor@manifoldengineering.ai
**Reply to:** AI's follow-up asking for parking

```
Hi Jill,

Parking is 60 spaces.

Lisa
```

### Claude Actions
```bash
python main.py 2>&1 | tee /tmp/e2e_final.log
```

**Expected:**
- Parking = 60 extracted
- All fields complete
- Closing email sent
- `row_completed`

---

## Phase 2.4: Handle Escalations (Dashboard)

### User Actions on Dashboard

1. **New Property (135 Trade Center)**
   - Click notification
   - Review and approve outreach to 150 Trade Center Court

2. **Tour Request (2017 St. Josephs)**
   - Click notification
   - Send suggested response or custom reply

3. **Identity Question (1 Randolph Ct)**
   - Click notification
   - Compose reply explaining confidential client

---

# FINAL VERIFICATION

## Success Checklist

### Part 1: Follow-Up Fix
- [ ] 21 follow-ups sent total (7 properties × 3 each)
- [ ] All follow-ups sent to brokers (`bp21harrison`, `manifold`)
- [ ] Zero follow-ups sent to self (`baylor.freelance`)

### Part 2: Core Functionality
- [ ] PDF data extracted (699 Industrial, 1800 Broad)
- [ ] Real PDF extracted (135 Trade Center brochure)
- [ ] Parking Spaces extracted (rows 3, 6, 8)
- [ ] AI asked for Parking when missing (row 9)
- [ ] Multi-turn conversation completed (row 9)

### Part 2: Escalations
- [ ] Identity question paused thread
- [ ] Tour request created notification
- [ ] Property unavailable moved to NON-VIABLE
- [ ] New property suggestion created approval flow

### Dashboard
- [ ] Notifications sorted by priority
- [ ] Stats cards show correct counts
- [ ] Conversations modal shows all threads

---

## Quick Reference: Broker Inbox Assignments

**bp21harrison@gmail.com (4 properties):**
- Row 3: 699 Industrial Park Dr
- Row 4: 135 Trade Center Court
- Row 7: 1 Randolph Ct
- Row 8: 1800 Broad St

**baylor@manifoldengineering.ai (3 properties):**
- Row 5: 2017 St. Josephs Drive
- Row 6: 9300 Lottsford Rd
- Row 9: 2525 Center West Pkwy

---

# BUG FIXES TO VERIFY (Next E2E)

## Fixed in Previous E2E (2026-03-08):
- [x] **Follow-up emails sent to self** - Fixed by adding explicit `toRecipients` in followup.py

## Fixed After Previous E2E (2026-03-09):
- [ ] **"Under contract" not triggering NON-VIABLE move** - Added missing keywords to processing.py:
  - `"under contract"`, `"went under contract"`, `"already leased"`
  - `"just leased"`, `"pending lease"`, `"contract pending"`
  - `"accepted an offer"`, `"lease signed"`, `"taken off market"`

## Configuration Reminder:
- [ ] **Set Parking Spaces to "Ask"** during column mapping (was set to "Skip" in previous test)

---

# FIRESTORE LIFECYCLE TRACKING

## Snapshot Commands (Run Before/During/After Campaign)

### Before Campaign Start
```bash
# Save Firestore state BEFORE campaign
python3 << 'EOF'
import json
from datetime import datetime
from google.cloud import firestore
import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "service-account.json"

db = firestore.Client()
uid = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"

snapshot = {
    "timestamp": datetime.utcnow().isoformat(),
    "phase": "BEFORE_CAMPAIGN",
    "threads": [],
    "notifications": {},
    "outbox": []
}

# Get threads
for t in db.collection(f"users/{uid}/threads").stream():
    data = t.to_dict()
    data["_id"] = t.id
    snapshot["threads"].append(data)

# Get outbox
for o in db.collection(f"users/{uid}/outbox").stream():
    data = o.to_dict()
    data["_id"] = o.id
    snapshot["outbox"].append(data)

# Get notifications per client
clients = list(db.collection(f"users/{uid}/clients").stream())
for c in clients:
    notifs = list(db.collection(f"users/{uid}/clients/{c.id}/notifications").stream())
    snapshot["notifications"][c.id] = len(notifs)

with open("/tmp/e2e_firestore_before.json", "w") as f:
    json.dump(snapshot, f, indent=2, default=str)

print(f"Saved snapshot: {len(snapshot['threads'])} threads, {len(snapshot['outbox'])} outbox items")
EOF
```

### After Each Phase
```bash
# Save Firestore state AFTER processing
python3 << 'EOF'
import json
from datetime import datetime
from google.cloud import firestore
import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "service-account.json"

db = firestore.Client()
uid = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
client_id = "<INSERT_CLIENT_ID>"  # Update with actual client ID

snapshot = {
    "timestamp": datetime.utcnow().isoformat(),
    "phase": "AFTER_REPLIES",  # Update phase name
    "threads": [],
    "notifications": [],
    "msgIndex_count": 0,
    "convIndex_count": 0
}

# Get threads for this client
for t in db.collection(f"users/{uid}/threads").stream():
    data = t.to_dict()
    if data.get("clientId") == client_id:
        snapshot["threads"].append({
            "id": t.id,
            "status": data.get("status"),
            "followUpStatus": data.get("followUpStatus"),
            "sheetRow": data.get("sheetRow"),
            "broker": data.get("broker")
        })

# Get notifications
for n in db.collection(f"users/{uid}/clients/{client_id}/notifications").stream():
    data = n.to_dict()
    snapshot["notifications"].append({
        "id": n.id,
        "kind": data.get("kind"),
        "rowAnchor": data.get("rowAnchor"),
        "priority": data.get("priority"),
        "reason": data.get("meta", {}).get("reason", "")
    })

# Count indexes
snapshot["msgIndex_count"] = len(list(db.collection(f"users/{uid}/msgIndex").stream()))
snapshot["convIndex_count"] = len(list(db.collection(f"users/{uid}/convIndex").stream()))

with open(f"/tmp/e2e_firestore_{snapshot['phase'].lower()}.json", "w") as f:
    json.dump(snapshot, f, indent=2, default=str)

print(f"Saved: {len(snapshot['threads'])} threads, {len(snapshot['notifications'])} notifications")
print(f"Indexes: {snapshot['msgIndex_count']} msgIndex, {snapshot['convIndex_count']} convIndex")
EOF
```

---

# POST-E2E ANALYSIS CHECKLIST

## 1. Log Analysis
```bash
# All backend logs saved to /tmp/e2e_*.log
ls -la /tmp/e2e_*.log

# Key things to grep for:
grep -c "✅ Sent" /tmp/e2e_*.log           # Emails sent
grep -c "📥 Scanned" /tmp/e2e_*.log        # Inbox scans
grep -c "❌" /tmp/e2e_*.log                 # Errors
grep -c "⚠️" /tmp/e2e_*.log                 # Warnings
grep "property_unavailable" /tmp/e2e_*.log  # NON-VIABLE triggers
```

## 2. Firestore Document Lifecycle
Compare snapshots:
- `e2e_firestore_before.json` - Before campaign
- `e2e_firestore_after_initial.json` - After initial send
- `e2e_firestore_after_followups.json` - After follow-ups
- `e2e_firestore_after_replies.json` - After broker replies

Check:
- [ ] Threads created at correct times
- [ ] Outbox items deleted after send
- [ ] Notifications created for each event
- [ ] msgIndex/convIndex growing as expected
- [ ] Thread statuses changing correctly (active → completed/paused)

## 3. Google Sheet State
- [ ] All expected rows highlighted yellow initially
- [ ] Completed rows un-highlighted
- [ ] NON-VIABLE divider exists
- [ ] Unavailable properties moved below divider
- [ ] All extracted data in correct columns
- [ ] Parking Spaces column populated (if configured)

## 4. Email Verification
- [ ] All initial emails in Outlook SentItems
- [ ] All follow-ups in SentItems with correct recipients
- [ ] AI replies in SentItems
- [ ] Closing emails sent for completed rows

## 5. Dashboard Verification
- [ ] Notifications appear in sidebar
- [ ] Priority ordering correct (action_needed first)
- [ ] Stats cards accurate
- [ ] Conversations modal shows all threads grouped by property
