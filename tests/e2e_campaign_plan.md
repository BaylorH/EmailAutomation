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
| 1 | 699 Industrial Park Dr | Jeff Wilson | Complete Info | 1 | Field extraction, row completion, closing email |
| 2 | 135 Trade Center Court | Luke Coffey | Multi-Turn Complete | 2 | Partial → complete, multi-turn accumulation |
| 3 | 2058 Gordon Hwy | Jonathan Aceves | Unavailable + Alt | 1 | property_unavailable event, new_property suggestion |
| 4 | 1 Kuhlke Dr | Robert McCrary | Escalation → Resume | 2 | needs_user_input, user reply, completion |
| 5 | 1 Randolph Ct | Scott Atkins | Wrong Contact | 1 | wrong_contact event, redirect handling |
| 6 | 1800 Broad St | Marcus Thompson | Property Issue | 1 | property_issue event, severity detection |
| 7 | 2525 Center West Pkwy | Lisa Anderson | Close Conversation | 1 | close_conversation event, exclusive scenario |

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
- [ ] Notification: `row_completed`
- [ ] AI sends closing/thank you email
- [ ] Row marked complete

---

### Scenario 2: Multi-Turn Complete (135 Trade Center Court)
**Goal:** Test partial info → follow-up → completion across 2 turns

**Turn 1 - Broker Reply:**
```
From: bp21harrison@gmail.com
Subject: Re: 135 Trade Center Court - Space Availability

Hi Jill,

Yes, 135 Trade Center Court is available. It's 32,500 SF total with 6 dock doors and 2 drive-ins.

I need to check on the other specs - will get back to you.

Luke
```

**Expected Results (Turn 1):**
- [ ] Sheet updates: Total SF=32500, Docks=6, Drive Ins=2
- [ ] Notification: `sheet_update` (multiple)
- [ ] AI sends follow-up requesting: Ops Ex, Ceiling Ht, Power
- [ ] Row NOT complete (missing fields)

**Turn 2 - Broker Reply:**
```
From: bp21harrison@gmail.com
Subject: Re: 135 Trade Center Court - Space Availability

Hey Jill,

Here's the rest of the info:
- NNN/CAM is $2.10/SF
- Clear height is 24 feet
- 800 amp 3-phase service

Let me know if you need anything else!

Luke
```

**Expected Results (Turn 2):**
- [ ] Sheet updates: Ops Ex=2.10, Ceiling Ht=24, Power=800 amps 3-phase
- [ ] Notification: `row_completed`
- [ ] AI sends closing email
- [ ] Row marked complete

---

### Scenario 3: Unavailable + New Property (2058 Gordon Hwy)
**Goal:** Test property_unavailable event AND new_property suggestion flow

**Turn 1 - Broker Reply:**
```
From: baylor@manifoldengineering.ai
Subject: Re: 2058 Gordon Hwy - Industrial Space

Hi Jill,

Unfortunately 2058 Gordon Hwy just went under contract last week - sorry about that!

However, I have another property that might work for your client - 3100 Peach Orchard Rd. It's a similar size warehouse, about 38,000 SF with good dock access. My colleague Sarah Chen handles that listing - you can reach her at sarah@meybohm.com.

Let me know if you'd like an introduction.

Jonathan
```

**Expected Results:**
- [ ] Event: `property_unavailable`
- [ ] Event: `new_property` with address="3100 Peach Orchard Rd", contactName="Sarah Chen", contactEmail="sarah@meybohm.com"
- [ ] Notification: `property_unavailable`
- [ ] Notification: `action_needed` (new_property_pending_approval)
- [ ] Row moved below NON-VIABLE divider
- [ ] **UI Test:** NewPropertyRequestModal appears with pre-filled data
- [ ] **UI Test:** User can approve/reject the new property suggestion

---

### Scenario 4: Escalation → User Reply → Complete (1 Kuhlke Dr)
**Goal:** Test needs_user_input escalation, thread pause, user intervention, and resume

**Turn 1 - Broker Reply:**
```
From: baylor@manifoldengineering.ai
Subject: Re: 1 Kuhlke Dr - Warehouse Space

Hi Jill,

I'd be happy to provide details on 1 Kuhlke Dr. Before I do, can you tell me a bit about your client's business? What type of operation are they running and what's their timeline for moving in?

Also, what's their budget range for the space?

Thanks,
Robert McCrary
```

**Expected Results (Turn 1):**
- [ ] Event: `needs_user_input` with reason `client_question`
- [ ] Notification: `action_needed` with meta showing the questions
- [ ] NO auto-reply sent (thread paused)
- [ ] **UI Test:** Notification sidebar shows action needed
- [ ] **UI Test:** Clicking notification opens chat interface
- [ ] **UI Test:** User composes reply via AI chat or manual edit

**Turn 2 - User Reply (via Frontend):**
User composes reply through the UI:
```
Hi Robert,

My client is in the distribution business and looking to move within the next 3-4 months. They're flexible on budget - mainly focused on finding the right space with good loading access.

Could you share the property specs when you get a chance?

Thanks,
Jill
```

- [ ] **UI Test:** User clicks "Send Email" in modal
- [ ] Outbox entry created
- [ ] Workflow sends the email
- [ ] Thread resumes

**Turn 3 - Broker Reply:**
```
From: baylor@manifoldengineering.ai
Subject: Re: 1 Kuhlke Dr - Warehouse Space

Perfect, that helps! Here's the info on 1 Kuhlke Dr:

- 28,000 SF
- $5.50/SF NNN
- $1.75 CAM
- 3 dock doors, 1 drive-in
- 22' clear
- 400 amps

Great loading access - perfect for distribution. Available in 60 days.

Robert
```

**Expected Results (Turn 3):**
- [ ] Sheet updates: All fields populated
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
| 135 Trade Center Court | Complete | All fields filled (after 2 turns) |
| 2058 Gordon Hwy | Non-Viable | Moved below divider |
| 1 Kuhlke Dr | Complete | All fields filled (after escalation) |
| 1 Randolph Ct | Action Needed | Wrong contact, pending user action |
| 1800 Broad St | Action Needed | Property issue flagged |
| 2525 Center West Pkwy | Closed | Conversation ended (exclusive) |

### 3.2 Notification Summary
Expected notifications by end of test:

| Type | Count | Properties |
|------|-------|------------|
| `row_completed` | 3 | 699 Industrial, 135 Trade Center, 1 Kuhlke |
| `sheet_update` | Multiple | Various field updates |
| `property_unavailable` | 1 | 2058 Gordon Hwy |
| `action_needed` | 4 | New property, escalation, wrong contact, property issue |
| `conversation_closed` | 1 | 2525 Center West |

### 3.3 Campaign Completion
- [ ] 3 rows complete (required fields filled)
- [ ] 1 row non-viable
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
