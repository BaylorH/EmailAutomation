# E2E Test Plan - Production Readiness Evaluation

**Date:** March 2026
**Test File:** `test_pdfs/E2E_Test_Augusta.xlsx`
**Follow-up Config:** 3 follow-ups (2 min, 3 min, 2 min)

---

## Quick Commands

```bash
# Status check
python tests/e2e_helpers.py status

# Trigger workflow
python tests/e2e_helpers.py trigger

# Check workflow status
python tests/e2e_helpers.py workflow

# Clear all data
python tests/e2e_helpers.py clear
```

---

## Test Data Matrix

### Properties & Contacts

| # | Property | Contact | Email | Test Scenario |
|---|----------|---------|-------|---------------|
| 1 | 100 Commerce Way | Tom Wilson | bp21harrison@gmail.com | Complete with PDFs |
| 2 | 200 Industrial Blvd | Sarah Miller | bp21harrison@gmail.com | Multi-turn (partial → complete) |
| 3 | 300 Warehouse Dr | Mike Chen | bp21harrison@gmail.com | Unavailable + New Property |
| 4 | 400 Distribution Ave | James Roberts | baylor@manifoldengineering.ai | Identity Question (pause) |
| 5 | 500 Logistics Ln | Karen Davis | baylor@manifoldengineering.ai | Tour Offer (pause) |
| 6 | 600 Storage Ct | Bob Thompson | baylor@manifoldengineering.ai | STOP CONVERSATION TEST |

### PDF Data Reference

| Property | SF | Rent | OpEx | Height | Docks | Drive-ins | Power |
|----------|-----|------|------|--------|-------|-----------|-------|
| 100 Commerce Way | 25,000 | $5.50 | $1.75 | 24 ft | 4 | 2 | 800 amps |
| 200 Industrial Blvd | 18,000 | $6.25 | $2.00 | 22 ft | 3 | 1 | 600 amps |
| 350 Tech Park Dr* | 22,000 | $5.75 | $1.85 | 20 ft | 3 | 2 | 500 amps |
| 400 Distribution Ave | 30,000 | $5.95 | $1.90 | 26 ft | 6 | 2 | 1000 amps |

*350 Tech Park Dr is the NEW PROPERTY suggested when 300 Warehouse Dr is unavailable

---

## Phase 1: Campaign Setup

### Actions
1. [ ] Clear existing data: `python tests/e2e_helpers.py clear`
2. [ ] Upload `test_pdfs/E2E_Test_Augusta.xlsx`
3. [ ] Set follow-up: **3 follow-ups** (2 min, 3 min, 2 min)
4. [ ] Start campaign

### Verify
```bash
python tests/e2e_helpers.py status
```

**Expected:**
- [ ] 6 emails in outbox
- [ ] Client has `followUpConfig.enabled: true`
- [ ] 3 follow-ups configured

### 📝 Document
- Screenshot: Campaign start modal with follow-up settings

---

## Phase 2: Initial Email Send

### Actions
1. [ ] Trigger workflow: `python tests/e2e_helpers.py trigger`
2. [ ] Wait for completion: `python tests/e2e_helpers.py workflow`

### Verify
```bash
python tests/e2e_helpers.py status
```

**Expected:**
- [ ] 6 threads created
- [ ] All threads `status: active`
- [ ] All threads `followUpStatus: waiting`
- [ ] Outbox empty

### 📝 Document
- Screenshot: Outlook sent folder (6 emails from jill@mohrpartners.com)
- Screenshot: Firestore threads collection

---

## Phase 3: Broker Replies (Batch 1 - All at once)

**Send ALL these replies simultaneously, then trigger ONE workflow run.**

### Reply 1: 100 Commerce Way - COMPLETE INFO + PDFs
**From:** bp21harrison@gmail.com
**To:** jill@mohrpartners.com
**Subject:** Re: 100 Commerce Way, Augusta
**Attach:** `100 Commerce Way - Property Flyer.pdf`, `100 Commerce Way - Floor Plan.pdf`

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

**Expected:** `status: completed`, closing email sent, `row_completed` notification

---

### Reply 2: 200 Industrial Blvd - PARTIAL INFO
**From:** bp21harrison@gmail.com
**To:** jill@mohrpartners.com
**Subject:** Re: 200 Industrial Blvd, Evans

```
Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```

**Expected:** Partial extraction, AI requests remaining, `status: active`

---

### Reply 3: 300 Warehouse Dr - UNAVAILABLE + NEW PROPERTY
**From:** bp21harrison@gmail.com
**To:** jill@mohrpartners.com
**Subject:** Re: 300 Warehouse Dr, Augusta

```
Hi Jill,

Unfortunately, 300 Warehouse Dr just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. Let me know if you want details.

Mike Chen
```

**Expected:** `property_unavailable`, row moved to NON-VIABLE, `new_property` notification

---

### Reply 4: 400 Distribution Ave - IDENTITY QUESTION
**From:** baylor@manifoldengineering.ai
**To:** jill@mohrpartners.com
**Subject:** Re: 400 Distribution Ave, Augusta

```
Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```

**Expected:** `status: paused`, `action_needed` notification (reason: `needs_user_input:confidential`)

---

### Reply 5: 500 Logistics Ln - TOUR OFFER
**From:** baylor@manifoldengineering.ai
**To:** jill@mohrpartners.com
**Subject:** Re: 500 Logistics Ln, Evans

```
Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```

**Expected:** `status: paused`, `action_needed` notification (reason: `tour_requested`)

---

### Reply 6: 600 Storage Ct - DO NOT REPLY (Stop Test)
**Do NOT send a reply for this property. We will use it to test the STOP button.**

---

### Post-Batch Actions
1. [ ] Send all 5 replies above
2. [ ] Trigger workflow: `python tests/e2e_helpers.py trigger`
3. [ ] Wait for completion

### Verify
```bash
python tests/e2e_helpers.py status
```

**Expected Results:**

| Property | Status | FollowUp | Notification |
|----------|--------|----------|--------------|
| 100 Commerce Way | `completed` | N/A | `row_completed` |
| 200 Industrial Blvd | `active` | waiting | `sheet_update` |
| 300 Warehouse Dr | `active` | - | `property_unavailable`, `action_needed` (new property) |
| 400 Distribution Ave | `paused` | paused | `action_needed` |
| 500 Logistics Ln | `paused` | paused | `action_needed` |
| 600 Storage Ct | `active` | waiting | None |

### 📝 Document
- Screenshot: Google Sheet with extracted data
- Screenshot: Notifications sidebar (priority sorted)
- Screenshot: Outlook inbox (AI auto-replies)
- Screenshot: Outlook sent folder (closing email for 100 Commerce Way)

---

## Phase 4: Test STOP Conversation

### Actions
1. [ ] Open Conversations modal for the client
2. [ ] Find "600 Storage Ct" thread (should be active)
3. [ ] Click "Stop" button
4. [ ] Confirm in dialog

### Verify
```bash
python tests/e2e_helpers.py threads
```

**Expected:**
- [ ] 600 Storage Ct: `status: stopped`
- [ ] 600 Storage Ct: `followUpStatus: paused`
- [ ] Badge shows "Stopped" (gray)

### 📝 Document
- Screenshot: Conversations modal showing status badges
- Screenshot: Stop confirmation dialog
- Screenshot: Thread after stopping (gray badge)

---

## Phase 5: Test Follow-up Emails

### Wait for Follow-up Timing
- 200 Industrial Blvd (active, waiting) should get follow-up after 2 minutes

### Actions
1. [ ] Wait 2+ minutes
2. [ ] Trigger workflow: `python tests/e2e_helpers.py trigger`

### Verify
```bash
python tests/e2e_helpers.py threads
```

**Expected:**
- [ ] 200 Industrial Blvd: `followUpConfig.currentFollowUpIndex: 1`
- [ ] Follow-up email sent in Outlook
- [ ] 600 Storage Ct: NO follow-up (stopped)

### 📝 Document
- Screenshot: Outlook sent folder (follow-up email)
- Screenshot: Firestore thread showing updated followUpConfig

---

## Phase 6: Batch 2 - Complete Remaining

### Reply 2b: 200 Industrial Blvd - COMPLETE REMAINING
**From:** bp21harrison@gmail.com
**To:** jill@mohrpartners.com
**Subject:** Re: 200 Industrial Blvd, Evans
**Attach:** `200 Industrial Blvd - Property Flyer.pdf`, `200 Industrial Blvd - Floor Plan.pdf`

```
Hi Jill,

Here's the rest of the info on 200 Industrial Blvd:

- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached flyer and floor plan.

Sarah
```

**Expected:** Remaining fields extracted, `status: completed`, closing email

---

### Actions
1. [ ] Send reply above
2. [ ] Trigger workflow

### Verify
- [ ] 200 Industrial Blvd: `status: completed`
- [ ] Closing email sent
- [ ] `row_completed` notification

---

## Phase 7: Test Additional Follow-ups (Optional)

If time permits, wait for follow-up #2 and #3 to verify the chain works.

---

## Phase 8: Final Verification

### Campaign Summary

| Property | Final Status | Data Complete | Notes |
|----------|-------------|---------------|-------|
| 100 Commerce Way | `completed` | ✅ | PDF extraction worked |
| 200 Industrial Blvd | `completed` | ✅ | Multi-turn completed |
| 300 Warehouse Dr | `active` | ❌ | NON-VIABLE, new property pending |
| 400 Distribution Ave | `paused` | ❌ | Waiting for user (identity) |
| 500 Logistics Ln | `paused` | ❌ | Waiting for user (tour) |
| 600 Storage Ct | `stopped` | ❌ | Manually stopped |

### 📝 Final Documentation
- Screenshot: Final Google Sheet state
- Screenshot: All notifications
- Screenshot: Conversations modal with all statuses
- Export: Firestore data snapshot

---

## Production Readiness Rubric

| Category | Test | Pass | Fail | Notes |
|----------|------|------|------|-------|
| **Email Delivery** | All 6 initial emails sent | | | |
| **Thread Matching** | Replies matched to correct threads | | | |
| **PDF Extraction** | Data extracted from attachments | | | |
| **Sheet Updates** | Correct fields populated | | | |
| **Status Tracking** | Threads show correct status | | | |
| **Pausing** | Escalations pause auto-reply | | | |
| **Stopping** | Manual stop works | | | |
| **Follow-ups** | Sent after 2 min delay | | | |
| **Multiple Follow-ups** | All 3 configured work | | | |
| **Notifications** | Correct types, priority sorted | | | |
| **Closing Emails** | Sent when row complete | | | |
| **NON-VIABLE** | Unavailable properties moved | | | |
| **New Property** | Suggestion creates notification | | | |

**Score:** ___/13

---

## Cleanup

```bash
python tests/e2e_helpers.py clear
```
