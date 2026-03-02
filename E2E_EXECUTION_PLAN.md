# E2E Campaign Test - Full Execution Plan

## Overview

This document provides the **exact sequence** of steps for the E2E test, with clear markers for:
- 🧑 **USER ACTION** - You do this
- 🤖 **CLAUDE AUDIT** - I verify/check this
- ⏸️ **CHECKPOINT** - We sync before continuing
- 🖥️ **MODAL TEST** - UI component to verify

---

# PHASE 0: Baseline Setup

## Step 0.1 - Pre-Test Snapshot
🤖 **CLAUDE AUDIT:**
- [ ] Verify Firestore access working
- [ ] Verify Google Sheets access working
- [ ] Verify Outlook API access working
- [ ] Document current state (any existing data)

⏸️ **CHECKPOINT:** Claude confirms all systems ready

---

# PHASE 1: Campaign Launch

## Step 1.1 - Create New Client

🧑 **USER ACTION:**
1. Go to Dashboard → "Add Client" or "Manage Clients"
2. Click to create new client

🖥️ **MODAL TEST: AddClientModal**
- [ ] Modal opens correctly
- [ ] File upload area visible

🧑 **USER ACTION:**
3. Upload `Scrub Augusta GA.xlsx`
4. Wait for file processing

⏸️ **CHECKPOINT:** Tell Claude "File uploaded, moving to column mapping"

---

## Step 1.2 - Column Mapping

🖥️ **MODAL TEST: ColumnMappingStep**
- [ ] Columns detected correctly
- [ ] Mapping UI displays all columns
- [ ] Can toggle columns on/off
- [ ] Can set gather vs track mode

🧑 **USER ACTION:**
1. Review column mappings
2. Ensure these are set to GATHER:
   - Total SF
   - Ops Ex /SF
   - Drive Ins
   - Docks
   - Ceiling Ht
   - Power
3. Ensure these are set to TRACK (not requested):
   - Rent/SF /Yr
   - Flyer / Link
   - Floorplan
4. Click Continue/Confirm

⏸️ **CHECKPOINT:** Tell Claude "Column mapping complete, client created"

---

## Step 1.3 - Claude Verifies Client Creation

🤖 **CLAUDE AUDIT:**
```
1. Firestore Check:
   - [ ] Client document exists in users/{uid}/clients/
   - [ ] Client has correct name
   - [ ] Client has status = 'new'
   - [ ] Client has excelUrl (Google Sheet created)

2. Google Sheet Check:
   - [ ] Sheet accessible via API
   - [ ] Headers match expected columns
   - [ ] 5 property rows exist (rows 3-7)
   - [ ] Row data matches Scrub file
```

⏸️ **CHECKPOINT:** Claude confirms "Client created successfully, ready to launch campaign"

---

## Step 1.4 - Launch Campaign

🧑 **USER ACTION:**
1. Find the new client in Dashboard or Clients list
2. Click "Get Started" button

🖥️ **MODAL TEST: StartProjectModal**
- [ ] Modal opens with client data
- [ ] Shows list of brokers/contacts
- [ ] Email script preview visible
- [ ] Personalization ([NAME]) shows correctly
- [ ] Can review all 5 outreach emails

🧑 **USER ACTION:**
3. Review email scripts
4. Click "Start Campaign" / "Send Emails"

⏸️ **CHECKPOINT:** Tell Claude "Campaign launched, emails queued"

---

## Step 1.5 - Claude Verifies Outbox Created

🤖 **CLAUDE AUDIT:**
```
Firestore Check:
- [ ] 5 outbox entries created (one per property)
- [ ] Each entry has: assignedEmails, script, clientId, rowNumber, subject
- [ ] Subjects match: "699 Industrial Park Dr, Evans", etc.
```

⏸️ **CHECKPOINT:** Claude confirms "Outbox ready, trigger the workflow"

---

## Step 1.6 - Trigger Workflow & Process Outbox

🧑 **USER ACTION:**
1. Go to GitHub Actions
2. Manually trigger `email.yml` workflow
3. Wait for completion
4. Copy FULL workflow logs
5. Paste logs to Claude

🤖 **CLAUDE AUDIT (from logs):**
```
- [ ] All 5 emails sent successfully
- [ ] Each shows "Sent and indexed email to..."
- [ ] Thread IDs created for each
- [ ] No errors in logs
```

🤖 **CLAUDE AUDIT (Firestore):**
```
- [ ] Outbox is now EMPTY
- [ ] 5 thread documents created
- [ ] Each thread has correct rowNumber (3, 4, 5, 6, 7)
- [ ] msgIndex entries created
- [ ] convIndex entries created
```

🤖 **CLAUDE AUDIT (Outlook):**
```
- [ ] 5 emails in Sent Items
- [ ] Correct recipients for each
- [ ] Subjects match properties
```

🤖 **CLAUDE AUDIT (Google Sheet):**
```
- [ ] No changes to data yet (awaiting replies)
- [ ] Row positions: 3, 4, 5, 6, 7
- [ ] No NON-VIABLE divider yet
```

⏸️ **CHECKPOINT:** Claude provides full Phase 1 report, confirms ready for broker replies

---

# PHASE 2: Broker Reply Scenarios

## Scenario A: Complete Info (1 Turn)
**Property:** 699 Industrial Park Dr (Row 3)

### Step A.1 - Send Broker Reply

🧑 **USER ACTION:**
1. Open bp21harrison@gmail.com
2. Find the email thread "699 Industrial Park Dr, Evans"
3. Reply with this EXACT text:

```
Hi Jill,

Happy to help! Here's the info on 699 Industrial Park Dr:

- Total SF: 15,000
- Ceiling Height: 24' clear
- Docks: 2 dock-high doors
- Drive-ins: 1 grade-level door
- Power: 400 amps, 3-phase
- Ops Ex: $2.50/SF NNN
- Asking Rent: $7.50/SF/yr

The space is available immediately. I can send over the flyer if you'd like.

Best,
Jeff Wilson
```

4. Send the email

**NOTE:** Broker voluntarily provides rent - AI should capture it but NEVER request rent in any response.

⏸️ **CHECKPOINT:** Tell Claude "Scenario A reply sent"

### Step A.2 - Process Reply

🧑 **USER ACTION:**
1. Trigger GitHub workflow
2. Paste full logs

🤖 **CLAUDE AUDIT (from logs):**
```
- [ ] Message matched to correct thread
- [ ] AI extracted: Total SF=15000, Ceiling Ht=24, Docks=2, Drive Ins=1, Power=400 amps 3-phase, Ops Ex=2.50
- [ ] AI extracted: Rent/SF /Yr=7.50 (voluntarily provided)
- [ ] Response type: closing (all fields complete)
- [ ] Closing email sent
```

🤖 **CLAUDE AUDIT (Firestore):**
```
- [ ] Thread has 3 messages (outbound, inbound, outbound reply)
- [ ] sheet_update notifications created
- [ ] row_completed notification created
```

🤖 **CLAUDE AUDIT (Google Sheet):**
```
Row 3 should have:
- [ ] Total SF: 15000
- [ ] Ceiling Ht: 24
- [ ] Docks: 2
- [ ] Drive Ins: 1
- [ ] Power: 400 amps, 3-phase
- [ ] Ops Ex /SF: 2.50
- [ ] Rent/SF /Yr: 7.50 (captured voluntarily)
- [ ] Gross Rent: AUTO-CALCULATED (formula, NOT written by AI)
```

🤖 **CRITICAL VERIFICATION (Forbidden Fields):**
```
- [ ] Check AI response email - does NOT ask for rent
- [ ] Gross Rent column is formula: =(H+I)*G/12
- [ ] AI never wrote to Gross Rent column
```

🤖 **CLAUDE AUDIT (Outlook):**
```
- [ ] Closing/thank you email sent to bp21harrison@gmail.com
- [ ] Email is threaded (RE: 699 Industrial Park Dr)
```

🖥️ **MODAL TEST: NotificationsSidebar**
🧑 **USER ACTION:**
1. Click notification bell on Dashboard
2. Verify notifications appear for 699 Industrial Park Dr

- [ ] sheet_update notifications visible
- [ ] row_completed notification visible
- [ ] Can click to view details

⏸️ **CHECKPOINT:** Claude confirms "Scenario A PASSED" or lists issues

---

## Scenario B: Partial → Complete (2 Turns)
**Property:** 135 Trade Center Court (Row 4)

### Step B.1 - Send Partial Info Reply

🧑 **USER ACTION:**
1. Open bp21harrison@gmail.com
2. Find thread "135 Trade Center Court, Augusta"
3. Reply with:

```
Hi,

The space at 135 Trade Center Court is 12,000 SF with 20' clear ceiling height.

I'll have to check on the other details and get back to you.

Thanks,
Luke
```

4. Send the email

⏸️ **CHECKPOINT:** Tell Claude "Scenario B Turn 1 sent"

### Step B.2 - Process Turn 1

🧑 **USER ACTION:**
1. Trigger workflow
2. Paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] Extracted: Total SF=12000, Ceiling Ht=20
- [ ] Response type: missing_fields
- [ ] Follow-up email sent requesting remaining fields

Firestore:
- [ ] Thread has 3 messages
- [ ] sheet_update notifications for SF and Ceiling

Sheet Row 4:
- [ ] Total SF: 12000
- [ ] Ceiling Ht: 20
- [ ] Other fields still empty
```

⏸️ **CHECKPOINT:** Claude confirms "Turn 1 processed, send Turn 2"

### Step B.3 - Send Completing Info Reply

🧑 **USER ACTION:**
1. Reply to same thread with:

```
Got those details for you:
- 2 dock doors
- 1 drive-in
- Power: 200 amps, single phase
- NNN: $1.85/SF

That should be everything!

Luke
```

2. Send the email

⏸️ **CHECKPOINT:** Tell Claude "Scenario B Turn 2 sent"

### Step B.4 - Process Turn 2

🧑 **USER ACTION:**
1. Trigger workflow
2. Paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] Extracted: Docks=2, Drive Ins=1, Power=200 amps single phase, Ops Ex=1.85
- [ ] Response type: closing
- [ ] Closing email sent

Firestore:
- [ ] Thread has 5 messages total
- [ ] row_completed notification

Sheet Row 4 (complete):
- [ ] Total SF: 12000
- [ ] Ceiling Ht: 20
- [ ] Docks: 2
- [ ] Drive Ins: 1
- [ ] Power: 200 amps, single phase
- [ ] Ops Ex /SF: 1.85
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario B PASSED"

---

## Scenario C: Unavailable + New Property
**Property:** 2058 Gordon Hwy (Row 5)

### Step C.1 - Send Unavailable Reply

🧑 **USER ACTION:**
1. Open baylor@manifoldengineering.ai
2. Find thread "2058 Gordon Hwy, Augusta"
3. Reply with:

```
Hi,

Unfortunately 2058 Gordon Hwy just went under contract last week.

However, I do have another listing at 500 Bobby Jones Expressway that might work - it's 22,000 SF with similar specs. The contact there is Mike Johnson at mike@augusta-realty.com.

Would you like info on that one?

Best,
Jonathan
```

4. Send the email

⏸️ **CHECKPOINT:** Tell Claude "Scenario C unavailable reply sent"

### Step C.2 - Process Unavailable + New Property

🧑 **USER ACTION:**
1. Trigger workflow
2. Paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] property_unavailable event detected
- [ ] new_property event detected (500 Bobby Jones)
- [ ] NON-VIABLE divider created (or already exists)
- [ ] Row moved below divider
- [ ] action_needed notification created for new property

Firestore:
- [ ] 2058 Gordon Hwy thread rowNumber updated
- [ ] Other thread rowNumbers adjusted if needed
- [ ] property_unavailable notification exists
- [ ] action_needed notification with reason=new_property_pending_approval

Sheet:
- [ ] NON-VIABLE divider exists
- [ ] 2058 Gordon Hwy is BELOW divider
- [ ] Other rows shifted appropriately
- [ ] NO row yet for 500 Bobby Jones (pending approval)
```

⏸️ **CHECKPOINT:** Claude confirms "Unavailable processed, check Dashboard for approval modal"

### Step C.3 - Approve New Property

🖥️ **MODAL TEST: NewPropertyRequestModal**

🧑 **USER ACTION:**
1. Go to Dashboard
2. Click notification bell OR find action indicator on client row
3. Click on the new property notification

- [ ] Modal opens showing new property details
- [ ] Shows: 500 Bobby Jones Expressway, Augusta
- [ ] Shows: Contact Mike Johnson, mike@augusta-realty.com
- [ ] Shows: Referred by Jonathan Aceves
- [ ] Email preview/editor visible
- [ ] Approve and Decline buttons work

🧑 **USER ACTION:**
4. Review the suggested email
5. Click "Approve" / "Send Email"
6. Verify modal closes

⏸️ **CHECKPOINT:** Tell Claude "New property approved via modal"

### Step C.4 - Verify New Property Created

🤖 **CLAUDE AUDIT:**
```
Firestore:
- [ ] Outbox entry created for 500 Bobby Jones
- [ ] action_needed notification deleted

After workflow runs:
- [ ] New thread created for 500 Bobby Jones
- [ ] Email sent to mike@augusta-realty.com

Sheet:
- [ ] New row inserted for 500 Bobby Jones (above NON-VIABLE)
- [ ] Row number is correct (likely row 7 or 8)
```

🧑 **USER ACTION:**
1. Trigger workflow
2. Paste logs

🤖 **CLAUDE AUDIT:**
```
- [ ] Email to mike@augusta-realty.com sent
- [ ] Thread created and indexed
```

⏸️ **CHECKPOINT:** Claude confirms "New property email sent, wait for Mike's reply"

### Step C.5 - Mike Replies with Tour Offer

🧑 **USER ACTION:**
1. Open bp21harrison@gmail.com (simulating Mike)
2. Find thread "500 Bobby Jones Expressway, Augusta"
3. Reply with:

```
Hi,

Thanks for reaching out about 500 Bobby Jones Expressway. Here's what I have:

- Total SF: 22,000
- Ceiling: 20' clear
- Docks: 3 dock-high
- Drive-ins: 1
- Power: 600 amps, 3-phase
- OpEx: $2.25/SF

I'd love to show this space to your client. I have availability this Thursday or Friday afternoon. Would either of those work for a tour?

Mike Johnson
Augusta Commercial Realty
```

4. Send, trigger workflow, paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] Fields extracted: Total SF=22000, Ceiling Ht=20, Docks=3, Drive Ins=1, Power=600 amps 3-phase, Ops Ex=2.25
- [ ] `tour_requested` event detected
- [ ] AI did NOT auto-reply (paused for tour approval)

Firestore:
- [ ] action_needed notification with reason=tour_requested
- [ ] Suggested response email in notification meta

Sheet Row 7:
- [ ] Total SF: 22000
- [ ] Ceiling Ht: 20
- [ ] Docks: 3
- [ ] Drive Ins: 1
- [ ] Power: 600 amps, 3-phase
- [ ] Ops Ex /SF: 2.25
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario C Turn 2 complete - tour offer detected, proceed to Scenario F for tour handling"

---

## Scenario D: Long Conversation (5 Turns)
**Property:** 1 Kuhlke Dr (Row 6)

### Step D.1 - Turn 1: Vague Reply

🧑 **USER ACTION:**
1. Open baylor@manifoldengineering.ai
2. Find thread "1 Kuhlke Dr, Augusta"
3. Reply with:

```
Yeah we have that space available. Nice building.

Let me know if you want to discuss.

Robert
```

4. Send, trigger workflow, paste logs

🤖 **CLAUDE AUDIT:**
```
- [ ] No field updates (vague response)
- [ ] AI sends follow-up requesting specifics
- [ ] Thread has 3 messages
```

### Step D.2 - Turn 2: Partial Info

🧑 **USER ACTION:**
Reply with:
```
Sure thing. Off the top of my head:
- It's about 8,000 SF total
- Ceiling is around 18 feet I think

I'll need to check with property management on the other specs.

Robert
```

Trigger workflow, paste logs.

🤖 **CLAUDE AUDIT:**
```
- [ ] Extracted: Total SF=8000, Ceiling Ht=18
- [ ] AI requests remaining fields
- [ ] Thread has 5 messages
```

### Step D.3 - Turn 3: More Info + Question

🧑 **USER ACTION:**
Reply with:
```
Got some answers back:
- Power: 400 amps, 3-phase
- We do have a floorplan available

One thing to note - the current tenant is using about 2,000 SF for office buildout. Would that work for your client or do they need the full warehouse space?

Robert
```

Trigger workflow, paste logs.

🤖 **CLAUDE AUDIT:**
```
- [ ] Extracted: Power=400 amps 3-phase
- [ ] Notes added about office buildout
- [ ] AI either answers contextually OR escalates
- [ ] Thread has 7 messages
```

### Step D.4 - Turn 4: More Fields

🧑 **USER ACTION:**
Reply with:
```
Good to know that works. Here are the remaining details:

- Docks: 1 dock door
- Drive-ins: 2 drive-in doors
- OpEx: $3.15/SF NNN

The space can be available in 60 days with some notice.

Robert
```

Trigger workflow, paste logs.

🤖 **CLAUDE AUDIT:**
```
- [ ] Extracted: Docks=1, Drive Ins=2, Ops Ex=3.15
- [ ] Thread has 9 messages
```

### Step D.5 - Turn 5: Final Details

🧑 **USER ACTION:**
Reply with:
```
Almost forgot - here's the flyer link: https://example.com/1kuhlke-flyer.pdf

Asking rent is $6.25/SF/yr.

Let me know if your client wants to tour.

Robert
```

Trigger workflow, paste logs.

🤖 **CLAUDE AUDIT:**
```
- [ ] Extracted: Flyer link, Rent (noted but not requested)
- [ ] All required fields complete
- [ ] Closing email sent
- [ ] row_completed notification
- [ ] Thread has 11+ messages (5+ back-and-forth)
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario D PASSED - 5+ turn conversation maintained"

---

## Scenario E: Escalation (Identity Question)
**Property:** 1 Randolph Ct (Row 7)

### Step E.1 - Send Identity Question

🧑 **USER ACTION:**
1. Open bp21harrison@gmail.com
2. Find thread "1 Randolph Ct, Evans"
3. Reply with:

```
Hi,

Before I share more details, can you tell me who your client is? We typically need to know who we're working with before providing specific pricing.

Thanks,
Scott
```

4. Send, trigger workflow, paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] needs_user_input event detected
- [ ] Subreason: confidential (or identity)
- [ ] NO automatic reply sent

Firestore:
- [ ] action_needed notification created
- [ ] reason = needs_user_input:confidential
```

⏸️ **CHECKPOINT:** Claude confirms "Escalation detected, check Dashboard"

### Step E.2 - Respond to Escalation via ChatWithAI Modal

🖥️ **MODAL TEST: Escalation Response with ChatWithAI**

🧑 **USER ACTION:**
1. Go to Dashboard
2. Click notification bell
3. Find the 1 Randolph Ct escalation notification
4. Click to open

**ChatWithAI Interface Verification:**
- [ ] Modal shows the broker's question clearly
- [ ] Shows full conversation context (thread history)
- [ ] Chat interface visible (not just text input)
- [ ] Proactive message from AI asking user what to respond
- [ ] Can type message to AI and get suggestions
- [ ] Email preview shows what will be sent

🧑 **USER ACTION:**
5. In chat, type: "Tell them we represent a confidential tenant looking for warehouse space"
6. AI should generate a professional response
7. Review the generated email in preview
8. Click Send (or edit and send)

**UX Verification:**
- [ ] Modal closes IMMEDIATELY after clicking Send (not waiting for async)
- [ ] Outbox entry created
- [ ] Toast/notification confirms "Email queued"

⏸️ **CHECKPOINT:** Tell Claude "Escalation response sent via ChatWithAI modal"

### Step E.3 - Process User Response

🧑 **USER ACTION:**
1. Trigger workflow
2. Paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] Response sent as REPLY to existing thread
- [ ] "Sending outbox item as REPLY to thread" in logs

Firestore:
- [ ] Message added to thread
- [ ] action_needed notification deleted

Outlook:
- [ ] Reply sent to bp21harrison@gmail.com
- [ ] Threaded correctly (RE: 1 Randolph Ct)
```

⏸️ **CHECKPOINT:** Claude confirms "User response sent as threaded reply"

### Step E.4 - Broker Provides Info (Different Person Signs)

🧑 **USER ACTION:**
Reply with (note: Sarah signs, not Scott):

```
Thanks for clarifying - industrial distribution makes sense for this space.

Here's what I have:
- Total SF: 18,500
- Ceiling Height: 24' clear
- Docks: 2 dock-high doors
- Drive-ins: 1 grade-level door
- NNN: $2.85/SF
- Power: 200 amps

Let me know if you need anything else.

Best,
Sarah
```

Trigger workflow, paste logs.

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] Fields extracted correctly
- [ ] Leasing Contact NOT updated (still Scott A. Atkins)

Sheet Row 7:
- [ ] Total SF: 18500
- [ ] Ceiling Ht: 24
- [ ] Docks: 2
- [ ] Drive Ins: 1
- [ ] Ops Ex /SF: 2.85
- [ ] Power: 200 amps
- [ ] Leasing Contact: Scott A. Atkins (UNCHANGED)
- [ ] Email: bp21harrison@gmail.com (UNCHANGED)
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario E PASSED - escalation handled, contact preserved"

---

## Scenario F: Tour Requested (500 Bobby Jones)
**Property:** 500 Bobby Jones Expressway (created in Scenario C)

### Step F.1 - Process Tour Offer

This continues from Scenario C - Mike's reply included a tour offer.

🤖 **CLAUDE AUDIT (from Scenario C Turn 2 logs):**
```
- [ ] `tour_requested` event detected
- [ ] `action_needed` notification created with reason=tour_requested
- [ ] Suggested response email generated
- [ ] AI did NOT auto-reply (waiting for user)
```

### Step F.2 - Respond to Tour Request via Modal

🖥️ **MODAL TEST: Tour Request Response Modal**

🧑 **USER ACTION:**
1. Go to Dashboard notification sidebar
2. Find the `tour_requested` notification for 500 Bobby Jones
3. Click to open

**Tour Modal Verification:**
- [ ] Modal shows tour context ("Thursday or Friday afternoon")
- [ ] Pre-filled suggested response visible
- [ ] Can edit the suggested response
- [ ] Shows "from Mike Johnson" context

🧑 **USER ACTION:**
4. Review or edit the suggested response
5. Click Send/Approve

**UX Verification:**
- [ ] Modal closes immediately
- [ ] Outbox entry created for response

🧑 **USER ACTION:**
6. Trigger workflow
7. Paste logs

🤖 **CLAUDE AUDIT:**
```
- [ ] Response sent as threaded reply
- [ ] Message indexed in thread
- [ ] action_needed notification resolved/deleted
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario F PASSED - tour request handled"

---

## Scenario G: Contact Optout (Edge Case)
**Property:** 699 Industrial Park Dr (already completed)
**Purpose:** Test what happens when broker says "remove me" after conversation

### Step G.1 - Send Optout Reply

🧑 **USER ACTION:**
1. Open bp21harrison@gmail.com
2. Find the CLOSED thread "699 Industrial Park Dr, Evans"
3. Reply with:

```
Please remove me from your mailing list. We don't work with tenant reps.

Jeff Wilson
```

4. Send, trigger workflow, paste logs

🤖 **CLAUDE AUDIT:**
```
Logs:
- [ ] `contact_optout` event detected
- [ ] Subreason: `no_tenant_reps`
- [ ] Contact added to opt-out list
- [ ] action_needed notification created

Firestore:
- [ ] Opt-out entry created (check optouts collection or user settings)
- [ ] Notification has reason=contact_optout:no_tenant_reps
```

⏸️ **CHECKPOINT:** Claude confirms "Scenario G PASSED - optout handled"

---

## Real-Time Notification Test

🖥️ **NOTIFICATION REAL-TIME TEST**

🧑 **USER ACTION:**
1. Open Dashboard in browser (keep it open)
2. Send a broker reply from another device/browser
3. Trigger workflow

**Verification:**
- [ ] Notification appears in sidebar WITHOUT page refresh
- [ ] Notification count updates in real-time
- [ ] Bell icon shows indicator

⏸️ **CHECKPOINT:** Confirm real-time notifications working

---

# PHASE 3: Final Verification

## Step 3.1 - Full State Audit

🤖 **CLAUDE AUDIT:**

### Firestore
```
Threads (6 total):
- [ ] 699 Industrial Park: Complete, 3+ messages, optout received
- [ ] 135 Trade Center: Complete, 5+ messages
- [ ] 2058 Gordon Hwy: Closed (non-viable), 2+ messages
- [ ] 1 Kuhlke Dr: Complete, 11+ messages (5+ turns)
- [ ] 1 Randolph Ct: Complete, 5+ messages (escalation handled)
- [ ] 500 Bobby Jones: Complete or active, tour handled

All rowNumbers:
- [ ] No duplicates
- [ ] All point to valid rows
- [ ] Properly adjusted after NON-VIABLE move
```

### Google Sheet
```
Final Row Layout:
- Row 3: 699 Industrial Park Dr ✓ COMPLETE (with rent captured)
- Row 4: 135 Trade Center Court ✓ COMPLETE
- Row 5: 1 Kuhlke Dr ✓ COMPLETE
- Row 6: 1 Randolph Ct ✓ COMPLETE (Leasing Contact unchanged)
- Row 7: 500 Bobby Jones Expressway ✓ (new property, tour handled)
- Row 8: NON-VIABLE DIVIDER
- Row 9: 2058 Gordon Hwy (moved below divider)

Column Verification:
- [ ] Gross Rent column has FORMULA (=(H+I)*G/12), not AI-written values
- [ ] Rent/SF /Yr has value for row 3 (voluntarily provided)
- [ ] Leasing Contact unchanged for all rows
```

### Outlook
```
Sent Items contain:
- [ ] Initial outreach to all 5 properties (5 emails)
- [ ] Follow-up/closing emails for completed properties
- [ ] Outreach to new property contact (Mike)
- [ ] User's escalation response (threaded, RE: 1 Randolph Ct)
- [ ] Tour response (threaded, RE: 500 Bobby Jones)
- [ ] Total sent count matches expected
```

### Notifications Summary
```
Types created:
- [ ] sheet_update - for each field extraction
- [ ] row_completed - for each completed property (4-5)
- [ ] property_unavailable - for 2058 Gordon Hwy
- [ ] action_needed (new_property) - created and resolved
- [ ] action_needed (needs_user_input) - created and resolved
- [ ] action_needed (tour_requested) - created and resolved
- [ ] action_needed (contact_optout) - for Jeff Wilson optout
```

### Campaign Completion Check
```
- [ ] All viable properties resolved (complete OR closed)
- [ ] Non-viable properties below divider
- [ ] No pending action_needed notifications
- [ ] Client status reflects completion percentage
```

## Step 3.2 - UI Final Check

🖥️ **MODAL TEST: ConversationsModal**

🧑 **USER ACTION:**
1. Find a completed client row
2. Click "View Conversations" (or similar)

- [ ] Modal shows all conversation threads
- [ ] Can expand each thread to see messages
- [ ] Messages display correctly (no formatting issues)
- [ ] Thread count matches expected

---

# PHASE 4: Final Report

🤖 **CLAUDE GENERATES:**

```markdown
# E2E Test Report - [DATE]

## Summary
- Properties Tested: 6 (5 original + 1 new)
- Scenarios Completed: 7/7
- Edge Cases Tested: 2
- Modals Tested: 6
- Issues Found: X

## Results by Scenario

| Scenario | Status | Turns | Key Verifications |
|----------|--------|-------|-------------------|
| A: Complete Info + Rent | ✅/❌ | 1 | All fields + voluntary rent captured, closing sent |
| B: Partial→Complete | ✅/❌ | 2 | Multi-turn extraction, follow-up sent |
| C: Unavailable+New | ✅/❌ | 2+ | Row moved, new property approval flow |
| D: Long Conversation | ✅/❌ | 5 | 5+ turn thread integrity maintained |
| E: Escalation | ✅/❌ | 3+ | Pause/resume, ChatWithAI modal, contact preserved |
| F: Tour Requested | ✅/❌ | 2 | Tour modal, suggested response, approval flow |
| G: Contact Optout | ✅/❌ | 1 | Optout detection, contact added to blocklist |

## Feature Verification

| Feature | Status | Notes |
|---------|--------|-------|
| Campaign launch (client + sheet + outbox) | ✅/❌ | |
| Outbox processing & email sending | ✅/❌ | |
| Thread creation & indexing | ✅/❌ | |
| AI field extraction | ✅/❌ | |
| Multi-turn conversations (2 turn) | ✅/❌ | |
| Multi-turn conversations (5+ turn) | ✅/❌ | |
| Property completion detection | ✅/❌ | |
| NON-VIABLE row handling | ✅/❌ | |
| Row number synchronization | ✅/❌ | |
| New property approval flow | ✅/❌ | |
| Escalation detection (needs_user_input) | ✅/❌ | |
| Escalation response (threaded reply) | ✅/❌ | |
| Tour requested detection | ✅/❌ | |
| Tour suggested response | ✅/❌ | |
| Contact optout detection | ✅/❌ | |
| Leasing Contact preservation | ✅/❌ | Different person signed email |
| Voluntary rent capture (not requested) | ✅/❌ | |
| Forbidden field protection (Gross Rent) | ✅/❌ | |
| Notification accuracy | ✅/❌ | |
| Real-time notification updates | ✅/❌ | |

## Modal UX Verification

| Modal | Tested | Closes Immediately | Notes |
|-------|--------|-------------------|-------|
| AddClientModal | ✅/❌ | ✅/❌ | |
| ColumnMappingStep | ✅/❌ | N/A | |
| StartProjectModal | ✅/❌ | ✅/❌ | |
| NewPropertyRequestModal | ✅/❌ | ✅/❌ | |
| ChatWithAI (Escalation) | ✅/❌ | ✅/❌ | |
| Tour Request Modal | ✅/❌ | ✅/❌ | |
| ConversationsModal | ✅/❌ | N/A | |
| NotificationsSidebar | ✅/❌ | N/A | |

## Event Types Tested

| Event | Detected | Handled | Notes |
|-------|----------|---------|-------|
| property_unavailable | ✅/❌ | ✅/❌ | |
| new_property | ✅/❌ | ✅/❌ | |
| needs_user_input:confidential | ✅/❌ | ✅/❌ | |
| tour_requested | ✅/❌ | ✅/❌ | |
| contact_optout:no_tenant_reps | ✅/❌ | ✅/❌ | |
| row_completed | ✅/❌ | ✅/❌ | |

## Issues Found
[List any issues discovered during testing]

## Production Readiness Assessment

### Critical Checks
- [ ] All scenarios passed
- [ ] No data corruption
- [ ] All modals close immediately (UX)
- [ ] Threading maintained throughout
- [ ] Forbidden fields protected

### Recommendation
[ ] ✅ READY FOR PRODUCTION - All tests passed
[ ] ⚠️ READY WITH CAVEATS - Minor issues (list)
[ ] ❌ NOT READY - Critical issues must be addressed first
```

---

# Quick Reference: Email Accounts

| Account | Role |
|---------|------|
| baylor.freelance@outlook.com | System (sends all outbound) |
| bp21harrison@gmail.com | Broker simulator (Jeff, Luke, Scott, Sarah) |
| baylor@manifoldengineering.ai | Broker simulator (Jonathan, Robert) |
