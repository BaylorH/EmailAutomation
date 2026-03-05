# E2E Test Scoring Template
## Campaign: E2E Test Campaign - Augusta GA

**Test Date:** _______________
**Tester:** _______________
**Follow-up Config:** 3 follow-ups (2 min, 3 min, 2 min)

---

# TEST DATA REFERENCE

## Properties & Contacts

| Row | Property | City | Contact | Email | Scenario |
|-----|----------|------|---------|-------|----------|
| 3 | 100 Commerce Way | Augusta | Tom Wilson | bp21harrison@gmail.com | Complete + PDFs |
| 4 | 200 Industrial Blvd | Evans | Sarah Miller | bp21harrison@gmail.com | Multi-turn |
| 5 | 300 Warehouse Dr | Augusta | Mike Chen | bp21harrison@gmail.com | Unavailable + New Property |
| 6 | 400 Distribution Ave | Augusta | James Roberts | baylor@manifoldengineering.ai | Identity Question |
| 7 | 500 Logistics Ln | Evans | Karen Davis | baylor@manifoldengineering.ai | Tour Offer |
| 8 | 600 Storage Ct | Augusta | Bob Thompson | baylor@manifoldengineering.ai | STOP Test |

## Expected Data Values (from PDFs)

| Property | Total SF | Rent/SF | OpEx/SF | Docks | Drive-Ins | Ceiling Ht | Power |
|----------|----------|---------|---------|-------|-----------|------------|-------|
| 100 Commerce Way | 25000 | 5.50 | 1.75 | 4 | 2 | 24 | 800 |
| 200 Industrial Blvd | 18000 | 6.25 | 2.00 | 3 | 1 | 22 | 600 |
| 350 Tech Park Dr* | 22000 | 5.75 | 1.85 | 3 | 2 | 20 | 500 |
| 400 Distribution Ave | 30000 | 5.95 | 1.90 | 6 | 2 | 26 | 1000 |

*350 Tech Park Dr = NEW PROPERTY suggested for 300 Warehouse Dr

## PDF Attachments to Use

| Reply | Attachments (from test_pdfs/pdfs/full_e2e/) |
|-------|---------------------------------------------|
| Reply 1 (100 Commerce) | `100 Commerce Way - Property Flyer.pdf`, `100 Commerce Way - Floor Plan.pdf` |
| Reply 2b (200 Industrial) | `200 Industrial Blvd - Property Flyer.pdf`, `200 Industrial Blvd - Floor Plan.pdf` |

---

# PHASE 1: CAMPAIGN SETUP

## Actions
- [ ] Clear existing data: `python3 tests/e2e_helpers.py clear`
- [ ] Upload `test_pdfs/E2E_Test_Augusta.xlsx` via UI
- [ ] Set follow-up: **3 follow-ups** (2 min, 3 min, 2 min)
- [ ] Start campaign

## Firestore State Check (After Setup)

```bash
python3 tests/e2e_helpers.py status
```

| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| Outbox count | 6 items | | |
| Client created | "E2E Test Campaign - Augusta GA" | | |
| followUpConfig.enabled | true | | |
| followUpConfig.followUps.length | 3 | | |
| No threads yet | 0 | | |
| No notifications yet | 0 | | |

### Firestore Quality Check
| Issue | Found? | Details |
|-------|--------|---------|
| Extra/unexpected fields on client doc | | |
| Duplicate outbox entries | | |
| Missing required fields | | |

---

# PHASE 2: INITIAL EMAIL SEND

## Actions
- [ ] Trigger workflow: `python3 tests/e2e_helpers.py trigger`
- [ ] Wait for completion: `python3 tests/e2e_helpers.py workflow`

## Firestore State Check (After Send)

| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| Threads created | 6 | | |
| Outbox count | 0 (all sent) | | |
| All threads status | "active" | | |
| All threads followUpStatus | "waiting" | | |
| All threads have contactName | Yes | | |
| All threads have rowNumber | Yes (3-8) | | |

### Thread Data Quality

| Thread | contactName | rowNumber | clientId | status | Extra fields? |
|--------|-------------|-----------|----------|--------|---------------|
| 100 Commerce Way | Tom Wilson | 3 | ✓ | active | |
| 200 Industrial Blvd | Sarah Miller | 4 | ✓ | active | |
| 300 Warehouse Dr | Mike Chen | 5 | ✓ | active | |
| 400 Distribution Ave | James Roberts | 6 | ✓ | active | |
| 500 Logistics Ln | Karen Davis | 7 | ✓ | active | |
| 600 Storage Ct | Bob Thompson | 8 | ✓ | active | |

### Outlook Check (baylor.freelance SentItems)
| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| Emails sent | 6 | | |
| All subjects correct format | "[Property], [City]" | | |
| Professional greeting | "Hi [FirstName]" | | |

---

# PHASE 3: BROKER REPLIES (Batch 1)

## Reply Scripts

### Reply 1: 100 Commerce Way - COMPLETE INFO
**From:** bp21harrison@gmail.com
**To:** baylor.freelance@outlook.com (replying to Jill's email)
**Subject:** Re: 100 Commerce Way, Augusta
**Attachments:** `100 Commerce Way - Property Flyer.pdf`, `100 Commerce Way - Floor Plan.pdf`

```
Hi Jill,

Happy to help! Here's everything on 100 Commerce Way:

- Total Size: 25,000 SF
- Asking Rate: $5.50/SF NNN
- Operating Expenses: $1.75/SF
- Loading: 4 dock doors, 2 drive-ins
- Clear Height: 24 ft
- Power: 800 amps, 3-phase

Available immediately. See attached flyer and floor plan.

Tom Wilson
```

---

### Reply 2: 200 Industrial Blvd - PARTIAL INFO
**From:** bp21harrison@gmail.com
**Subject:** Re: 200 Industrial Blvd, Evans
**Attachments:** None

```
Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```

---

### Reply 3: 300 Warehouse Dr - UNAVAILABLE + NEW PROPERTY
**From:** bp21harrison@gmail.com
**Subject:** Re: 300 Warehouse Dr, Augusta
**Attachments:** None

```
Hi Jill,

Unfortunately, 300 Warehouse Dr just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. Let me know if you want details.

Mike Chen
```

---

### Reply 4: 400 Distribution Ave - IDENTITY QUESTION
**From:** baylor@manifoldengineering.ai
**Subject:** Re: 400 Distribution Ave, Augusta
**Attachments:** None

```
Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```

---

### Reply 5: 500 Logistics Ln - TOUR OFFER
**From:** baylor@manifoldengineering.ai
**Subject:** Re: 500 Logistics Ln, Evans
**Attachments:** None

```
Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```

---

### Reply 6: 600 Storage Ct - NO REPLY
**Do NOT send a reply. This tests the STOP button.**

---

## Actions
- [ ] Send replies 1-5 (not 6)
- [ ] Trigger workflow: `python3 tests/e2e_helpers.py trigger`
- [ ] Wait for completion

## Firestore State Check (After Batch 1)

| Thread | Status | FollowUp | msgIndex Count | Pass? |
|--------|--------|----------|----------------|-------|
| 100 Commerce Way | completed | N/A | 3 (out, in, closing) | |
| 200 Industrial Blvd | active | waiting | 3 (out, in, reply) | |
| 300 Warehouse Dr | active | paused | 2 (out, in) | |
| 400 Distribution Ave | paused | paused | 2 (out, in) | |
| 500 Logistics Ln | paused | paused | 2 (out, in) | |
| 600 Storage Ct | active | waiting | 1 (out only) | |

### Notification Check

| Notification | Property | Reason | Exists? | Correct? |
|--------------|----------|--------|---------|----------|
| row_completed | 100 Commerce Way | All fields filled | | |
| sheet_update (multiple) | 200 Industrial Blvd | Partial data | | |
| property_unavailable | 300 Warehouse Dr | Under contract | | |
| action_needed | 300 Warehouse Dr | new_property_pending_approval | | |
| action_needed | 400 Distribution Ave | needs_user_input:confidential | | |
| action_needed | 500 Logistics Ln | tour_requested | | |

### Notification Quality Check

| Issue | Found? | Details |
|-------|--------|---------|
| Duplicate notifications | | |
| Missing expected notifications | | |
| Wrong notification type | | |
| Notification for wrong property | | |
| Extra/unexpected notifications | | |

---

# CONVERSATION QUALITY ANALYSIS

## 100 Commerce Way Conversation

### Messages (copy from Outlook)

**Message 1 - Initial Outreach (Jill → Tom):**
```
[Paste actual email content here]
```

**Message 2 - Broker Reply (Tom → Jill):**
```
[Paste actual email content here]
```

**Message 3 - Closing Email (Jill → Tom):**
```
[Paste actual email content here]
```

### Quality Scoring (1-5 scale)

| Criteria | Score | Notes |
|----------|-------|-------|
| **Greeting correct** (Hi Tom, not Hi [NAME]) | /5 | |
| **Natural flow** - reads like real conversation | /5 | |
| **Professional tone** - concise, polite | /5 | |
| **No AI tells** - no robotic language | /5 | |
| **Rule compliance** - didn't reveal client | /5 | |
| **Closing appropriate** - thanked for info | /5 | |

**Total: ___/30**

**Red flags found:**
- [ ] Literal [NAME] in email
- [ ] Weird vocabulary
- [ ] Repeated information
- [ ] Asked for info already provided
- [ ] Revealed client identity
- [ ] Asked for rent (shouldn't ask)
- [ ] Other: _______________

---

## 200 Industrial Blvd Conversation (Multi-turn)

### Messages

**Message 1 - Initial Outreach:**
```
[Paste]
```

**Message 2 - Partial Reply (Sarah):**
```
[Paste]
```

**Message 3 - AI Follow-up Request:**
```
[Paste]
```

**Message 4 - Follow-up Email (if sent):**
```
[Paste]
```

### Quality Scoring

| Criteria | Score | Notes |
|----------|-------|-------|
| **Greeting correct** (Hi Sarah) | /5 | |
| **Only requested missing fields** (rate, power, opex) | /5 | |
| **Didn't repeat what Sarah provided** | /5 | |
| **Natural flow across multiple messages** | /5 | |
| **Follow-up personalized** (if sent) | /5 | |

**Total: ___/25**

---

## 400 Distribution Ave Conversation (Identity Question)

### Messages

**Message 1 - Initial Outreach:**
```
[Paste]
```

**Message 2 - Identity Question (James):**
```
[Paste]
```

**Message 3 - AI Response (should NOT exist - paused):**
```
[Should be empty - thread paused]
```

### Quality Scoring

| Criteria | Score | Notes |
|----------|-------|-------|
| **Thread correctly paused** - no auto-reply sent | /5 | |
| **Notification created with correct reason** | /5 | |

**Total: ___/10**

---

## 500 Logistics Ln Conversation (Tour Offer)

### Messages

**Message 1 - Initial Outreach:**
```
[Paste]
```

**Message 2 - Tour Offer (Karen):**
```
[Paste]
```

**Message 3 - AI Response (should NOT exist - paused):**
```
[Should be empty - thread paused]
```

### Quality Scoring

| Criteria | Score | Notes |
|----------|-------|-------|
| **Thread correctly paused** - no auto-reply sent | /5 | |
| **Notification created with tour_requested** | /5 | |

**Total: ___/10**

---

# PHASE 4: STOP CONVERSATION TEST

## Actions
- [ ] Open Conversations modal in UI
- [ ] Find "600 Storage Ct" thread
- [ ] Click "Stop" button
- [ ] Confirm dialog

## Firestore State Check (After Stop)

| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| 600 Storage Ct status | stopped | | |
| 600 Storage Ct followUpStatus | paused | | |
| UI badge shows | "Stopped" (gray) | | |

---

# PHASE 5: FOLLOW-UP TEST

## Actions
- [ ] Wait 2+ minutes
- [ ] Trigger workflow

## Follow-up Quality Check

| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| Follow-up sent for 200 Industrial | Yes | | |
| Follow-up greeting | "Hi Sarah" (not [NAME]) | | |
| Follow-up NOT sent for 600 Storage | Correct (stopped) | | |
| Follow-up NOT sent for 400/500 | Correct (paused) | | |
| Follow-up NOT sent for 100 Commerce | Correct (completed) | | |
| currentFollowUpIndex updated | 1 | | |

### Follow-up Email Content

```
[Paste actual follow-up email here]
```

### Follow-up Quality Scoring

| Criteria | Score | Notes |
|----------|-------|-------|
| **Correct recipient name** | /5 | |
| **Natural language** | /5 | |
| **Appropriate urgency** | /5 | |
| **Not repetitive** | /5 | |

**Total: ___/20**

---

# PHASE 6: COMPLETE 200 INDUSTRIAL

## Reply 2b Script
**From:** bp21harrison@gmail.com
**Subject:** Re: 200 Industrial Blvd, Evans
**Attachments:** `200 Industrial Blvd - Property Flyer.pdf`, `200 Industrial Blvd - Floor Plan.pdf`

```
Hi Jill,

Here's the rest of the info on 200 Industrial Blvd:

- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached flyer and floor plan.

Sarah
```

## Actions
- [ ] Send Reply 2b
- [ ] Trigger workflow

## Firestore State Check

| Check | Expected | Actual | Pass? |
|-------|----------|--------|-------|
| 200 Industrial status | completed | | |
| row_completed notification | Created | | |
| Closing email sent | Yes | | |

---

# FINAL STATE ANALYSIS

## Google Sheet Final State

| Property | Total SF | Rent | OpEx | Docks | Drive-Ins | Height | Power | Complete? |
|----------|----------|------|------|-------|-----------|--------|-------|-----------|
| 100 Commerce Way | 25000 | 5.50 | 1.75 | 4 | 2 | 24 | 800 | ✅ |
| 200 Industrial Blvd | 18000 | 6.25 | 2.00 | 3 | 1 | 22 | 600 | ✅ |
| 300 Warehouse Dr | - | - | - | - | - | - | - | ❌ (NON-VIABLE) |
| 400 Distribution Ave | - | - | - | - | - | - | - | ❌ (paused) |
| 500 Logistics Ln | - | - | - | - | - | - | - | ❌ (paused) |
| 600 Storage Ct | - | - | - | - | - | - | - | ❌ (stopped) |

### Sheet Quality Check

| Issue | Found? | Details |
|-------|--------|---------|
| Gross Rent formula overwritten | | |
| Numbers have units (should be plain) | | |
| Links show file:// instead of Drive URL | | |
| Wrong data in wrong row | | |
| 300 Warehouse NOT below NON-VIABLE | | |

---

## Firestore Final State

### Thread Summary

| Thread | Status | Messages | contactName | Extra Fields? |
|--------|--------|----------|-------------|---------------|
| 100 Commerce Way | completed | ? | Tom Wilson | |
| 200 Industrial Blvd | completed | ? | Sarah Miller | |
| 300 Warehouse Dr | active | ? | Mike Chen | |
| 400 Distribution Ave | paused | ? | James Roberts | |
| 500 Logistics Ln | paused | ? | Karen Davis | |
| 600 Storage Ct | stopped | ? | Bob Thompson | |

### Collection Counts

| Collection | Expected | Actual | Extra? |
|------------|----------|--------|--------|
| threads | 6 | | |
| msgIndex | ~15-18 | | |
| convIndex | 6 | | |
| processedMessages | 0 (cleared) | | |
| notifications | ~8-10 | | |

### Firestore Quality Issues

| Issue | Found? | Details |
|-------|--------|---------|
| Orphaned documents | | |
| Missing references | | |
| Bloated message counts | | |
| Duplicate entries | | |
| Stale data from old tests | | |

---

## Notification ↔ Conversation Alignment

| Notification | Triggering Event in Conversation | Aligned? |
|--------------|----------------------------------|----------|
| row_completed (100 Commerce) | Tom provided all info + PDFs | |
| row_completed (200 Industrial) | Sarah completed remaining fields | |
| sheet_update (multiple) | Sarah's partial info | |
| property_unavailable (300 Warehouse) | Mike said "under contract" | |
| action_needed:new_property | Mike suggested 350 Tech Park | |
| action_needed:confidential | James asked "who is client" | |
| action_needed:tour_requested | Karen offered Thursday/Friday tour | |

### Alignment Issues

| Issue | Found? | Details |
|-------|--------|---------|
| Notification without matching event | | |
| Event without notification | | |
| Wrong notification type for event | | |
| Duplicate notifications for same event | | |

---

# FINAL SCORES

## Functional Tests (Pass/Fail)

| Test | Pass | Fail |
|------|------|------|
| All 6 initial emails sent | | |
| Thread matching works | | |
| PDF extraction works | | |
| Sheet updates correct | | |
| Status tracking correct | | |
| Pausing works | | |
| Stopping works | | |
| Follow-ups sent correctly | | |
| Follow-up personalization (name) | | |
| Notifications created | | |
| Closing emails sent | | |
| NON-VIABLE movement | | |

**Functional Score: ___/12**

---

## Quality Scores

| Category | Score | Max |
|----------|-------|-----|
| 100 Commerce Conversation Quality | | /30 |
| 200 Industrial Conversation Quality | | /25 |
| 400 Distribution (Pause Correct) | | /10 |
| 500 Logistics (Pause Correct) | | /10 |
| Follow-up Email Quality | | /20 |
| Firestore Data Cleanliness | | /20 |
| Sheet Data Accuracy | | /20 |
| Notification Alignment | | /15 |

**Quality Score: ___/150**

---

## Firestore Cleanliness Rubric (20 points)

| Criteria | Points | Score |
|----------|--------|-------|
| No orphaned documents | 4 | |
| No duplicate entries | 4 | |
| All required fields present | 4 | |
| No extra/unnecessary fields | 4 | |
| Message counts match reality | 4 | |

---

## Sheet Accuracy Rubric (20 points)

| Criteria | Points | Score |
|----------|--------|-------|
| All extracted values correct | 5 | |
| Numbers formatted correctly | 5 | |
| Links stored properly | 3 | |
| Formulas not overwritten | 4 | |
| NON-VIABLE row placement correct | 3 | |

---

## Notification Alignment Rubric (15 points)

| Criteria | Points | Score |
|----------|--------|-------|
| All expected notifications exist | 5 | |
| No extra/duplicate notifications | 5 | |
| Correct types and reasons | 5 | |

---

# OVERALL ASSESSMENT

**Functional Score:** ___/12
**Quality Score:** ___/150

**Total: ___/162**

## Production Readiness

| Grade | Score Range | Ready? |
|-------|-------------|--------|
| A | 150+ | Yes - Ship it |
| B | 130-149 | Yes with minor fixes |
| C | 100-129 | Needs work |
| D | <100 | Not ready |

**Grade: ___**

## Critical Issues Found
1.
2.
3.

## Recommendations
1.
2.
3.

---

**Test Completed:** _______________
**Signed Off By:** _______________
