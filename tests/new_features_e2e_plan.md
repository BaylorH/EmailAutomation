# E2E Test Plan: New Features (March 2026)

## Overview

Testing the following new features implemented in this session:
1. **Thread Status System** - Tracks conversation state (active/paused/stopped/completed)
2. **Stop Conversation Button** - User can manually stop monitoring a thread
3. **Minutes-Based Follow-ups** - Follow-up timing now supports minutes (for testing)
4. **Notification Priority Sorting** - Notifications sorted by importance

---

## Pre-Test: Follow-up Config Bug Investigation

### Problem
Follow-up config not being saved when starting campaign.

### Steps
1. [ ] Freeze current E2E campaign (client: `wHhV6cJjzR2kjt10KfoX`)
2. [ ] Create new test client using `Scrub Augusta GA.xlsx`
3. [ ] In StartProjectModal, set follow-up to **2 minutes**
4. [ ] Start campaign (DO NOT trigger workflow yet)
5. [ ] Check Firestore: Does `followUpConfig` exist on client doc?
6. [ ] Check Firestore: Do threads have `followUpConfig.nextFollowUpAt`?

### Expected Result
- Client doc should have `followUpConfig.followUps[0].waitTime = 2, waitUnit = "minutes"`
- Threads should have `followUpStatus: "waiting"` and `followUpConfig.nextFollowUpAt` set

### If Bug Found
- Debug frontend: Check `StartProjectModal.jsx` where `followUpConfig` is built
- Debug backend: Check `email.py` where thread is created
- Fix and redeploy

### After Fix Confirmed
- Copy `followUpConfig` to frozen E2E campaign client
- Delete test client and all its data
- Continue with main E2E test

---

## Phase 1: Campaign Setup (Already Done)

### Client: E2E Test Campaign - Augusta GA
- **Client ID:** `wHhV6cJjzR2kjt10KfoX`
- **Sheet ID:** `13zoPMLinGqA4noBSyIhIVgDHohC7-n9xxxbqkBULanc`
- **Status:** live
- **Threads:** 6 (all status=active)

### Properties & Emails

| Property | Broker Email | Scenario |
|----------|--------------|----------|
| 100 Commerce Way | bp21harrison@gmail.com | Complete info (tests `status: completed`) |
| 200 Industrial Blvd | bp21harrison@gmail.com | Partial → complete (multi-turn) |
| 300 Warehouse Dr | bp21harrison@gmail.com | Unavailable + new property |
| 400 Distribution Ave | baylor@manifoldengineering.ai | Identity question (tests `status: paused`) |
| 500 Logistics Ln | baylor@manifoldengineering.ai | Tour offer (tests `status: paused`) |
| 600 Storage Ct | baylor@manifoldengineering.ai | Wrong contact |

---

## Phase 2: Broker Replies (Round 1)

All replies sent FROM test emails TO jill@mohrpartners.com.

### Reply 1: 100 Commerce Way - COMPLETE INFO
**From:** bp21harrison@gmail.com
**Attach:** 2 PDFs from `test_pdfs/pdfs/full_e2e/`

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

**Expected:**
- All fields extracted
- `row_completed` notification
- Thread `status: completed`
- Closing email sent

---

### Reply 2: 200 Industrial Blvd - PARTIAL INFO
**From:** bp21harrison@gmail.com
**No attachments**

```
Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```

**Expected:**
- Partial fields extracted (SF, ceiling, docks, drive-ins)
- `sheet_update` notifications
- AI requests remaining fields
- Thread `status: active` (still ongoing)

---

### Reply 3: 300 Warehouse Dr - UNAVAILABLE + NEW PROPERTY
**From:** bp21harrison@gmail.com
**No attachments**

```
Hi Jill,

Unfortunately, 300 Warehouse Dr just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. Let me know if you want details.

Mike Chen
```

**Expected:**
- `property_unavailable` event
- `new_property` event
- Row moved below NON-VIABLE
- `action_needed` notification (new property pending)
- Thread `status: active` or handled appropriately

---

### Reply 4: 400 Distribution Ave - IDENTITY QUESTION
**From:** baylor@manifoldengineering.ai
**No attachments**

```
Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```

**Expected:**
- `needs_user_input` event with reason `confidential`
- `action_needed` notification
- **Thread `status: paused`** ← NEW FEATURE TEST
- NO auto-reply sent

---

### Reply 5: 500 Logistics Ln - TOUR OFFER
**From:** baylor@manifoldengineering.ai
**No attachments**

```
Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```

**Expected:**
- `tour_requested` event
- `action_needed` notification
- **Thread `status: paused`** ← NEW FEATURE TEST
- NO auto-reply sent (or acknowledgment only)

---

### Reply 6: 600 Storage Ct - WRONG CONTACT
**From:** baylor@manifoldengineering.ai
**No attachments**

```
Hi Jill,

I no longer handle this property - I left Columbia Commercial last month. You'll want to reach out to Jennifer Adams who took over. Her email is jennifer.adams@columbiacommercial.com.

Good luck with your search!

Bob Thompson
```

**Expected:**
- `wrong_contact` event
- `action_needed` notification
- Contact info captured
- NO auto-reply sent

---

## Phase 3: Process & Verify (After Round 1)

### 3.1 Trigger Workflow
```
gh workflow run email.yml --repo BaylorH/EmailAutomation
```

### 3.2 Verify Thread Statuses

| Property | Expected Status | Expected Reason |
|----------|----------------|-----------------|
| 100 Commerce Way | `completed` | All fields gathered |
| 200 Industrial Blvd | `active` | Partial info, awaiting more |
| 300 Warehouse Dr | `active` | Unavailable handled |
| 400 Distribution Ave | `paused` | needs_user_input:confidential |
| 500 Logistics Ln | `paused` | tour_requested |
| 600 Storage Ct | `active` or `paused` | wrong_contact |

### 3.3 Verify Notifications (Priority Sorted)

Check NotificationsSidebar - should be sorted:
1. `action_needed` (important) - first
2. `row_completed` (important) - next
3. `property_unavailable` - then
4. `sheet_update` - last

### 3.4 Verify Google Sheet Updates
- 100 Commerce Way: All fields filled
- 200 Industrial Blvd: Partial fields filled
- 300 Warehouse Dr: Moved to NON-VIABLE section
- Others: Unchanged or notes added

---

## Phase 4: Test Stop Conversation Button

### Steps
1. [ ] Open ConversationsModal for the client
2. [ ] Verify status badges display correctly (Active/Paused)
3. [ ] Click "Stop" on 200 Industrial Blvd (active thread)
4. [ ] Confirm dialog appears
5. [ ] Click confirm

### Expected
- Thread status changes to `stopped`
- `followUpStatus` changes to `paused`
- Row highlight cleared in Google Sheet
- Badge updates to "Stopped" (gray)

### Verify in Firestore
```python
# Thread should have:
status: "stopped"
statusReason: "user_requested"
followUpStatus: "paused"
```

---

## Phase 5: Test Follow-up Timing (2 Minutes)

### Prerequisites
- Follow-up config must be set on client/threads
- At least one thread with no response for 2+ minutes

### Steps
1. [ ] Verify thread has `followUpConfig.nextFollowUpAt` set
2. [ ] Wait 2+ minutes
3. [ ] Trigger workflow
4. [ ] Check if follow-up email sent

### Expected
- Follow-up email sent to broker
- `followUpConfig.currentFollowUpIndex` incremented
- `followUpConfig.nextFollowUpAt` updated for next follow-up

---

## Phase 6: Round 2 Replies (Multi-turn Completion)

### Reply 7: 200 Industrial Blvd - COMPLETE REMAINING
**From:** bp21harrison@gmail.com
**Attach:** 2 PDFs

```
Hi Jill,

Here's the rest:
- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached flyer and floor plan.

Sarah
```

**Expected:**
- Remaining fields extracted
- `row_completed` notification
- Thread `status: completed`
- Closing email sent

---

## Phase 7: Final Verification

### Thread Status Summary

| Property | Final Status |
|----------|--------------|
| 100 Commerce Way | completed |
| 200 Industrial Blvd | completed (if not stopped) or stopped |
| 300 Warehouse Dr | active (non-viable) |
| 400 Distribution Ave | paused |
| 500 Logistics Ln | paused |
| 600 Storage Ct | active/paused |

### Notification Count

| Kind | Expected Count |
|------|----------------|
| row_completed | 2 (100, 200) |
| sheet_update | Multiple |
| action_needed | 3-4 |
| property_unavailable | 1 |

### New Feature Checklist

- [ ] Thread status field populated on all threads
- [ ] Status changes to `paused` on escalation events
- [ ] Status changes to `completed` on row completion
- [ ] Stop button works and sets status to `stopped`
- [ ] Stopped threads don't process new replies (message saved, no AI)
- [ ] Follow-ups work with minutes (if config fixed)
- [ ] Notifications sorted by priority in sidebar

---

## Cleanup

After testing complete:
- Delete test client (if created for debugging)
- Optionally delete E2E campaign or keep for future testing
