# E2E Campaign Test Plan - Full Production Simulation

## Overview

This test simulates a complete campaign lifecycle from file upload to campaign completion, testing all major features and user interactions.

---

## Phase 1: Campaign Setup

### 1.1 Upload & Column Mapping
- [ ] Upload `Scrub Augusta GA.xlsx` (7 properties)
- [ ] Verify column auto-detection works
- [ ] Confirm all 7 properties detected with valid emails
- [ ] Verify script sheet detected (primary + secondary scripts)

### 1.2 Campaign Launch (StartProjectModal)
- [ ] Verify signature loads correctly (not "no signature configured")
- [ ] Verify all 7 personalized emails generated
- [ ] Verify [NAME] placeholder replaced with first names
- [ ] **Configure follow-ups:** 3 follow-ups at 1 hour, 2 hours, 3 hours
- [ ] Click "Start Campaign" - all 7 emails queued to outbox

### 1.3 Email Sending (Backend Workflow)
- [ ] Trigger workflow
- [ ] Verify all 7 emails sent (check logs)
- [ ] Verify threads indexed in Firestore
- [ ] Verify client status = 'live'

---

## Phase 2: Broker Responses & AI Processing

### Properties & Test Scenarios

| # | Property | Contact | Scenario | Turns | Tests |
|---|----------|---------|----------|-------|-------|
| 1 | 699 Industrial Park Dr | Jeff Wilson | Complete Info + Custom Field | 1 | Field extraction, **Parking Spaces custom column**, row completion, closing email |
| 2 | 135 Trade Center Court, Augusta GA | Luke Coffey | **REAL** Complete + Call Offer | 1 | Real PDFs, floorplan detection, call_requested escalation |
| 3 | 2017 St. Josephs Drive, Bowie MD | Brian Greene | **REAL** Unavailable + Alt | 1 | property_unavailable, new_property with URL |
| 4 | 9300 Lottsford Rd, Largo MD | Craig Cheney | **REAL** Confidentiality Question | 2 | needs_user_input:confidential escalation |
| 5 | 1 Randolph Ct | Scott Atkins | Wrong Contact | 1 | wrong_contact event, redirect handling |
| 6 | 1800 Broad St | Marcus Thompson | Property Issue | 1 | property_issue event, severity detection |
| 7 | 2525 Center West Pkwy | Lisa Anderson | Close Conversation | 1 | close_conversation event, exclusive scenario |

**Note:** Scenarios 2, 3, 4 use REAL broker responses forwarded by Jill Ames from production emails.

---

## Detailed Conversation Scripts

### Scenario 1: Complete Info (699 Industrial Park Dr)
**Goal:** Test full field extraction and automatic closing

**Turn 1 - Broker Reply:**
```
From: bp21harrison@gmail.com
Subject: Re: 699 Industrial Park Dr - Industrial Space Inquiry

Hi Jill,

Thanks for reaching out about 699 Industrial Park Dr. Here are all the details:

- Total Size: 45,000 SF
- Asking Rate: $6.75/SF NNN
- Operating Expenses: $1.85/SF
- Loading: 4 dock-high doors, 2 drive-in doors
- Clear Height: 28'
- Power: 1200 amps, 480V 3-phase

The property is available immediately. I've attached the flyer for your reference.

Best regards,
Jeff Wilson
```

**Expected Results:**
- [ ] Sheet updates: Total SF=45000, Rent/SF=6.75, Ops Ex=1.85, Docks=4, Drive Ins=2, Ceiling Ht=28, Power=1200 amps 480V 3-phase
- [ ] **Custom field: Parking Spaces=85** (tests dynamic column extraction)
- [ ] Notification: `row_completed`
- [ ] AI sends closing/thank you email
- [ ] Row marked complete

---

### Scenario 2: REAL - Complete Info + Call Offer (135 Trade Center Court, Augusta GA)
**Goal:** Test real broker response with PDFs and call offer detection
**Source:** Jill Ames forwarded email from Luke Coffey, May 2025

**Turn 1 - REAL Broker Reply:**
```
From: Luke.Coffey@southeastern.company
Subject: Re: 135 Trade Center Court, Augusta, GA

Good Morning Jill,

This would certainly be a great fit here. Please see attached Building C & D plans.
We are asking $15/SF/NNN and we anticipate a delivery of July 1, 2025; building A is almost complete.

More than happy to jump on a call to discuss at your convenience. Just let me know what works best for you.

Luke Coffey
Sales Associate
Southeastern
p: (706)-854-6731
c: (651)-271-8098
```

**Attachments (REAL PDFs in test_pdfs/real_world/):**
- `Sealed Bldg C 10-24-23.pdf` (2.8MB) → Floorplan column
- `Sealed Bldg D 10-24-23.pdf` (2.9MB) → Floorplan column
- `135 Trade Center Court - Brochure.pdf` (804KB) → Flyer/Link column

**Expected Results:**
- [ ] Sheet updates: Rent/SF=15, Ops Ex=NNN
- [ ] PDF categorization: 2 files to Floorplan, 1 to Flyer/Link
- [ ] Event: `call_requested` (broker offered to "jump on a call")
- [ ] Notification: `action_needed` with call request
- [ ] NO auto-reply (paused for user to schedule call)
- [ ] Row NOT complete yet (missing SF, docks, ceiling, power)

---

### Scenario 3: REAL - Unavailable + New Property (2017 St. Josephs Drive, Bowie MD)
**Goal:** Test real property_unavailable + new_property suggestion with website URL
**Source:** Jill Ames forwarded email from Brian Greene, August 2025
**Property Name:** Woodmore Commons

**Turn 1 - REAL Broker Reply (initial):**
```
From: bg@hp-llc.com
Subject: Re: Woodmore Commons - 2017 St. Josephs Drive, Bowie, MD

Hi Jill. I'm sorry but we are already at lease for this space, and it's our last one.
We also have an anchor restriction on fitness concepts.

Thank you,
Brian Greene
EVP
```

**Turn 2 - Follow-up (Jill asked for alternatives):**
```
From: bg@hp-llc.com
Subject: Re: Woodmore Commons - 2017 St. Josephs Drive, Bowie, MD

Hi Jill. Below is the only current space we have and is about 10 miles from Woodmore.

https://www.hp-llc.com/the-centre-at-forestville

Brian Greene
EVP OF LEASING
703-725-1351
www.hp-llc.com
```

**Expected Results:**
- [ ] Event: `property_unavailable` (already at lease + anchor restriction)
- [ ] Event: `new_property` with URL: https://www.hp-llc.com/the-centre-at-forestville
- [ ] New property name: "The Centre at Forestville" (~10 miles from original)
- [ ] Notification: `property_unavailable`
- [ ] Notification: `action_needed` (new_property_pending_approval)
- [ ] Row moved below NON-VIABLE divider
- [ ] **UI Test:** NewPropertyRequestModal with property URL
- [ ] **Future:** Web scraping to extract property data from URL

---

### Scenario 4: REAL - Confidentiality Question (9300 Lottsford Rd, Largo MD)
**Goal:** Test needs_user_input:confidential escalation - AI must NOT reveal client identity
**Source:** Jill Ames forwarded email from Craig Cheney, August 2025
**Property Name:** The Tapestry

**Turn 1 - REAL Broker Reply:**
```
From: ccheney@KLNB.com
Cc: awillner@klnb.com
Subject: RE: The Tapestry - 9300 Lottsford Rd, Largo MD

Hi Jill,

There is plenty of free retail parking on the first level of the garage.
Attached is a JPG where I highlighted the floor plan of the 1,400 SF space.
This was taken from page 4 of the attached PDF.
Please let us know what else you need.

By the way, what franchise is it that you are working with?

Thanks,
Craig S. Cheney
O: 703-268-2705 | C: 703-399-1041
KLNB Commercial Real Estate Services
```

**Attachments (REAL PDFs in test_pdfs/real_world/):**
- `Tapestry Largo Station Retail Floor Plan.pdf` (5.1MB) → Floorplan column
- `1400 SF Floor Plan-Highlighted.jpg` (image)

**CRITICAL:** Jill asked "Can the A.I. Auto respond to that?" - Answer is **NO**.
The question "what franchise is it?" is asking about client identity = CONFIDENTIAL.

**Expected Results (Turn 1):**
- [ ] Sheet updates: Total SF=1400, Notes="free retail parking on first level"
- [ ] PDF: Floor Plan → Floorplan column
- [ ] Event: `needs_user_input` with reason `confidential`
- [ ] Notification: `action_needed` with subreason "confidential"
- [ ] **NO auto-reply sent** (thread paused)
- [ ] **UI Test:** Notification shows "Broker asked about client identity"
- [ ] **UI Test:** User must compose reply manually

**Turn 2 - User Reply (via Frontend):**
User composes reply (deciding how much to disclose):
```
Hi Craig,

Thanks for the floor plan! The parking situation sounds perfect.

My client is in the quick-service restaurant space. Could you provide the remaining specs like ceiling height, power, and any NNN/CAM costs?

Thanks,
Jill
```

- [ ] Outbox entry created by user
- [ ] Thread resumes after user sends

**Turn 3 - Broker completes info:**
```
From: ccheney@KLNB.com
Subject: RE: The Tapestry - 9300 Lottsford Rd, Largo MD

Great! QSR tenants do well here with the foot traffic.

Here are the specs:
- NNN: $12.50/SF
- Clear height: 14'
- 200 amp service
- No dock doors (retail)
- 1 rear entrance (drive-in equivalent)

Let me know if you'd like to schedule a tour.

Craig
```

**Expected Results (Turn 3):**
- [ ] Sheet updates: Ops Ex=12.50, Ceiling Ht=14, Power=200 amps, Drive Ins=1, Docks=0
- [ ] Notification: `row_completed`
- [ ] AI sends closing email
- [ ] Row complete

---

### Scenario 5: Wrong Contact (1 Randolph Ct)
**Goal:** Test wrong_contact event detection and handling

**Turn 1 - Broker Reply:**
```
From: bp21harrison@gmail.com
Subject: Re: 1 Randolph Ct - Commercial Space

Hi Jill,

I no longer handle the listing at 1 Randolph Ct - I left Atkins Commercial last month.

You'll want to reach out to Mike Stevens who took over my listings. His email is mike.stevens@atkinscommercial.com.

Good luck!
Scott
```

**Expected Results:**
- [ ] Event: `wrong_contact` with subreason `left_company`
- [ ] Notification: `action_needed` with redirect info
- [ ] Contact info captured: Mike Stevens, mike.stevens@atkinscommercial.com
- [ ] NO auto-reply sent
- [ ] **UI Test:** User sees notification with new contact info
- [ ] **UI Test:** User can manually update contact or dismiss

---

### Scenario 6: Property Issue (1800 Broad St)
**Goal:** Test property_issue event detection with severity

**Turn 1 - Broker Reply:**
```
From: bp21harrison@gmail.com
Subject: Re: 1800 Broad St - Industrial Space

Hi Jill,

Thanks for your interest in 1800 Broad St. I want to be upfront with you - we had some water damage in the rear section of the building from a roof leak last month. About 2,000 SF is affected.

We're currently getting repairs done and expect it to be completed in 3-4 weeks. The rest of the building (18,000 SF) is in good condition.

Here are the specs:
- Total: 20,000 SF (18K usable now)
- $5.25/SF NNN
- $1.50 CAM
- 2 docks, 2 drive-ins
- 20' clear
- 600 amps

Let me know if your client wants to wait for repairs or see the unaffected portion.

Marcus
```

**Expected Results:**
- [ ] Event: `property_issue` with severity `major` (water damage)
- [ ] Sheet updates: All fields populated
- [ ] Notification: `action_needed` with property issue details
- [ ] Notes captured: "Water damage in rear section, repairs in progress"
- [ ] **UI Test:** User notified of issue, can decide to proceed or not

---

### Scenario 7: Close Conversation - Exclusive (2525 Center West Pkwy)
**Goal:** Test close_conversation event for "going exclusive" scenario

**Turn 1 - Broker Reply:**
```
From: baylor@manifoldengineering.ai
Subject: Re: 2525 Center West Pkwy - Office/Warehouse

Hi Jill,

Thanks for reaching out about 2525 Center West Pkwy. Unfortunately, we've gone exclusive with another tenant rep on this property as of last week. They're working with a client who's close to signing.

I wish you and your client the best of luck in your search!

Lisa Anderson
```

**Expected Results:**
- [ ] Event: `close_conversation`
- [ ] Notes: "exclusive_with_another" or similar
- [ ] Notification: `conversation_closed`
- [ ] NO response email sent (conversation terminated)
- [ ] Row NOT moved to non-viable (property still exists, just not available to us)

---

## Phase 3: Final Verification

### 3.1 Sheet State Check
After all turns complete:

| Property | Status | Expected State |
|----------|--------|----------------|
| 699 Industrial Park Dr | Complete | All fields filled, row complete |
| 135 Trade Center Court, Augusta GA | Action Needed | Call offer - awaiting user to schedule |
| 2017 St. Josephs Drive, Bowie MD | Non-Viable | Moved below divider, new property suggested |
| 9300 Lottsford Rd, Largo MD | Complete | All fields filled (after confidential escalation) |
| 1 Randolph Ct | Action Needed | Wrong contact, pending user action |
| 1800 Broad St | Action Needed | Property issue flagged |
| 2525 Center West Pkwy | Closed | Conversation ended (exclusive) |

### 3.2 Notification Summary
Expected notifications by end of test:

| Type | Count | Properties |
|------|-------|------------|
| `row_completed` | 2 | 699 Industrial, 9300 Lottsford (The Tapestry) |
| `sheet_update` | Multiple | Various field updates |
| `property_unavailable` | 1 | 2017 St. Josephs (Woodmore Commons) |
| `action_needed` | 4 | Call offer (135 Trade), new property, confidential question, wrong contact, property issue |
| `conversation_closed` | 1 | 2525 Center West |

### 3.3 Campaign Completion
- [ ] 2 rows complete (required fields filled)
- [ ] 1 row non-viable
- [ ] 3 rows action needed (call, confidential, wrong contact, issue)
- [ ] 3 rows requiring user attention
- [ ] Campaign NOT auto-marked complete (pending items exist)

---

## Phase 4: User Action Resolution

### 4.1 Resolve New Property Suggestion (from Scenario 3)
- [ ] Open NewPropertyRequestModal for 3100 Peach Orchard Rd
- [ ] Verify contact pre-filled (Sarah Chen)
- [ ] Verify email pre-filled (sarah@meybohm.com)
- [ ] Compose/edit outreach email
- [ ] Click Send - new outbox entry created
- [ ] New row added to sheet for 3100 Peach Orchard Rd

### 4.2 Resolve Wrong Contact (Scenario 5)
- [ ] View notification with new contact info
- [ ] Option: Update contact in sheet and resend
- [ ] Option: Dismiss and mark resolved

### 4.3 Resolve Property Issue (Scenario 6)
- [ ] View property issue notification
- [ ] Discuss with client
- [ ] Option: Proceed (compose follow-up)
- [ ] Option: Dismiss (client not interested due to damage)

---

## Test Execution Checklist

### Round 1: Initial Replies (All 7 properties)
```
Trigger workflow after all replies sent
```

### Round 2: Multi-Turn Completion
```
Turn 2 for: 135 Trade Center Court (complete remaining fields)
Turn 2 for: 1 Kuhlke Dr (user reply via frontend)
Turn 3 for: 1 Kuhlke Dr (broker provides specs)
```

### Round 3: User Actions via UI
```
- Approve/send new property outreach
- Resolve wrong contact notification
- Resolve property issue notification
```

---

## Success Criteria

1. **100% Event Detection** - All expected events fired correctly
2. **100% Field Extraction** - All provided data captured accurately
3. **No Forbidden Actions** - Never writes to Gross Rent, Leasing Contact, Email
4. **Correct Escalation** - AI pauses on needs_user_input, resumes after user reply
5. **UI Modals Work** - All notification types open correct modals
6. **Multi-Turn Works** - Data accumulates correctly across turns
7. **No Duplicate Emails** - Each property gets exactly the expected emails
