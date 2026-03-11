# E2E Test Run Sheet - March 2026

## Campaign Configuration

### Test File
**USE:** `test_pdfs/E2E_Real_World_Test.xlsx`

This file already has:
- 7 properties with broker emails set to test accounts
- Custom columns: Rail Access, Office %, Building Condition Notes
- Script sheet with outreach template

### Basic Settings
| Setting | Value |
|---------|-------|
| Client Name | |
| From Email | baylor.freelance@outlook.com |
| Follow-up #1 | 5 days |
| Follow-up #2 | 3 days (8d total) |
| Follow-up #3 | 2 days (10d total) |
| Campaign Started | _(timestamp)_ |

### Column Configuration (during campaign setup)

**Standard Fields:**
| Field | Mode |
|-------|------|
| Total SF | Ask + Required ✓ |
| Ops Ex /SF | Ask + Required ✓ |
| Drive Ins | Ask + Required ✓ |
| Docks | Ask + Required ✓ |
| Ceiling Ht | Ask + Required ✓ |
| Power | Ask + Required ✓ |
| Flyer / Link | Ask + Required ✓ |
| Rent/SF /Yr | Skip (accept if provided, never ask) |

**Custom Fields (ALL 3 UI MODES TESTED):**
| Column Header | UI Setting | Internal Mode | Test Purpose |
|---------------|------------|---------------|--------------|
| **Rail Access** | Ask + Required ✓ | `ask_required` | Blocks closing if missing, AI must ask |
| **Office %** | Ask (no Required) | `ask_optional` | AI asks but doesn't block closing |
| **Building Condition Notes** | Note | `note` | Auto-append mentions, never asks |

### Pre-Campaign Checklist
- [ ] Upload `test_pdfs/E2E_Real_World_Test.xlsx`
- [ ] Configure Rail Access → Click "Ask", check "Required" toggle
- [ ] Configure Office % → Click "Ask", leave "Required" unchecked
- [ ] Configure Building Condition Notes → Click "Note"
- [ ] Set follow-ups to 5/3/2 days (realistic intervals)
- [ ] Clear bp21harrison@gmail.com inbox
- [ ] Clear baylor@manifoldengineering.ai inbox

---

# Property 1: 699 Industrial Park Dr

## Scenario: Complete standard + ALL custom field modes

### Broker Reply (send this)
```
Subject: RE: 699 Industrial Park Dr

Hey there -

So 699 Industrial Park is about 45k feet, we're asking 8.50
triple net on that one. Clear height's 28ft, got 4 dock doors
and 2 drive-ins. Power is 400 amp 3 phase.

Building is rail served - CSX spur runs right to the back dock.
About 15% is finished office space.

FYI the roof was replaced last year and HVAC is newer too.

Flyer's attached - let me know if you want to see it.

Thanks,
Bob Martinez
(covering for Rich this week)
```
**Attach**: PDF

### Expected Behavior
| Item | Expected |
|------|----------|
| Standard Fields | Total SF: 45000, Ops Ex: 8.50 (NNN), Ceiling: 28, Docks: 4, Drive Ins: 2, Power: 400A 3-phase |
| Flyer Link | Populated from PDF upload |
| **Rail Access** | "CSX spur" or similar ← ask_required ✓ |
| **Office %** | 15 ← ask_optional ✓ |
| **Building Condition Notes** | "Roof replaced last year, newer HVAC" ← note (auto-appended) |
| Leasing Contact | Should NOT change (stay as original, not "Bob Martinez") |
| AI Response | Closing email (all required fields including Rail Access are complete) |
| Events | `close_conversation` |
| Notification | `row_completed` |
| End State | COMPLETE |

### Actual Results
| Item | Actual |
|------|--------|
| Data Extracted | _(paste sheet values)_ |
| Flyer Link | |
| Rail Access | |
| Office % | |
| Building Condition Notes | |
| Leasing Contact | |
| AI Response | _(paste actual email)_ |
| Events Detected | |
| Notification Created | |
| End State | |

### Notes
```
(Did all 3 custom field modes work correctly?)
```

---

# Property 2: 1 Randolph Ct

## Scenario: Multi-turn + Custom Required Blocks Closing

**KEY TEST:** AI has all standard fields but NO Rail Access → should NOT close, must ask for Rail

### Broker Reply #1 (send this)
```
Subject: RE: 1 Randolph Ct

The space is 22,000 SF with 24' clear. NNN is $2.50/SF.
```
**No attachment**

### Expected After Reply #1
| Item | Expected |
|------|----------|
| Data Extracted | Total SF: 22000, Ceiling: 24 |
| AI Response | Request remaining: Ops Ex, Docks, Drive Ins, Power, Flyer |
| Follow-up Scheduled | Yes (5 min) |

### Actual After Reply #1
| Item | Actual |
|------|--------|
| Data Extracted | |
| AI Response | _(paste)_ |
| Follow-up Scheduled | |

---

### _(Wait for follow-up email to arrive)_

### Follow-up #1 Received?
| Item | Actual |
|------|--------|
| Time Received | |
| Content | _(paste)_ |

---

### Broker Reply #2 (send after follow-up)
```
Subject: RE: 1 Randolph Ct

Sorry for the delay - NNN is $2.15/SF. 3 docks.
Still checking on the power situation, will get back to you.
```
**No attachment**

### Expected After Reply #2
| Item | Expected |
|------|----------|
| Data Extracted | Ops Ex: 2.15, Docks: 3 (accumulated with previous) |
| AI Response | Request remaining: Drive Ins, Power, Flyer |
| Follow-up Scheduled | Yes (should resume) |

### Actual After Reply #2
| Item | Actual |
|------|--------|
| Data Extracted | |
| AI Response | _(paste)_ |
| Follow-up Scheduled | |

---

### _(Wait for follow-up #2 to arrive)_

### Follow-up #2 Received?
| Item | Actual |
|------|--------|
| Time Received | |
| Content | _(paste)_ |

---

### Broker Reply #3 (send after follow-up)
```
Subject: RE: 1 Randolph Ct

Power is 200A 3-phase. 1 drive-in door. Here's the flyer.
About 10% office space in the front.
```
**Attach**: PDF

### Expected After Reply #3
| Item | Expected |
|------|----------|
| Data Extracted | Power: 200A 3-phase, Drive Ins: 1, Flyer: populated |
| All Standard Fields | ✅ Complete (SF, Ceiling, Ops Ex, Docks, Drive Ins, Power, Flyer) |
| **Office %** | 10 ← ask_optional (extracted) |
| **Rail Access** | ❌ MISSING |
| AI Response | **Should ask for Rail Access** (NOT close - Rail is required!) |
| Events | (none - still gathering) |
| End State | WAITING (Rail Access missing) |

### Actual After Reply #3
| Item | Actual |
|------|--------|
| Data Extracted | |
| Office % | |
| AI Response | _(paste)_ |
| **Did AI ask for Rail Access?** | Yes / No |
| End State | |

### ⚠️ KEY TEST: Custom Required Field Blocks Closing
If AI sends closing email here, **Rail Access "ask_required" is NOT working!**

---

### Broker Reply #4 (send after AI asks for Rail)
```
Subject: RE: 1 Randolph Ct

No rail access at this location - closest rail is about 5 miles away.
```
**No attachment**

### Expected After Reply #4
| Item | Expected |
|------|----------|
| **Rail Access** | "No rail, closest 5 miles" or similar ✅ |
| AI Response | Closing email (NOW all required fields including Rail are complete) |
| Events | `close_conversation` |
| Notification | `row_completed` |
| End State | COMPLETE |

### Actual After Reply #4
| Item | Actual |
|------|--------|
| Rail Access | |
| AI Response | _(paste)_ |
| Events Detected | |
| Notification Created | |
| End State | |

### Notes
```
(Did ask_required correctly block closing until Rail Access provided?)
(Did accept_only extract Sprinkler without asking?)
```

---

# Property 3: 150 Trade Center Court

## Scenario: Unavailable + vague new property suggestion

### Broker Reply (send this)
```
Subject: RE: 150 Trade Center Court

Hey - bad news, 150 Trade Center just went under contract last week.

I might have something else that could work though - there's a
new development on Trade Center Court, similar specs. Want me
to send you info on that?
```
**No attachment**

### Expected Behavior
| Item | Expected |
|------|----------|
| Events | `property_unavailable` + `new_property` (or just unavailable with interest expressed) |
| 150 Row | Moved to NON-VIABLE |
| AI Response | Acknowledge unavailable, express interest in alternative |
| Notification | `property_unavailable` and/or `new_property_pending_approval` |

### Actual Results
| Item | Actual |
|------|--------|
| Events Detected | |
| 150 Row Location | |
| AI Response | _(paste)_ |
| Notification Created | |

### Notes
```
(how did AI handle the vague suggestion?)
```

---

# Property 3b: 135 Trade Center Court (New Property)

## Scenario: New property flow with PDF partial data

### _(After approving new property in UI)_

### New Row Created?
| Item | Actual |
|------|--------|
| Row Created | Yes / No |
| Address | |
| Outreach Sent | |
| Outreach Content | _(paste)_ |

---

### Broker Reply to 135 (send this)
```
Subject: RE: 135 Trade Center Court

Here's the info on 135 Trade Center Court - flyer attached.

It's a flex space, 6,000 SF per building. Let me know if
you need anything else.
```
**Attach**: 135 Trade Center Court - Brochure.pdf

### Expected Behavior
| Item | Expected |
|------|----------|
| PDF Extracted | Total SF: 6000, Rent: 14-15 (from PDF) |
| Flyer Link | Populated |
| AI Response | Request missing: Ops Ex, Docks, Drive Ins, Ceiling Ht, Power |
| Missing Fields | Ops Ex, Docks, Drive Ins, Ceiling Ht, Power |

### Actual Results
| Item | Actual |
|------|--------|
| PDF Data Extracted | |
| Flyer Link | |
| AI Response | _(paste)_ |

---

### Broker Reply #2 to 135 (send this)
```
Subject: RE: 135 Trade Center Court

Sure thing -

NNN is around $3/SF. No dock doors on these buildings,
2 drive-in doors each. Ceiling is 16' clear. Power is
200A single phase.

Let me know if that works for your client.
```
**No attachment**

### Expected After Reply #2
| Item | Expected |
|------|----------|
| Data Extracted | Ops Ex: 3, Docks: 0, Drive Ins: 2, Ceiling: 16, Power: 200A single phase |
| AI Response | Closing email |
| Events | `close_conversation` |
| Notification | `row_completed` |
| End State | COMPLETE |

### Actual After Reply #2
| Item | Actual |
|------|--------|
| Data Extracted | |
| AI Response | _(paste)_ |
| Events Detected | |
| Notification Created | |
| End State | |

---

# Property 4: 1800 Broad St

## Scenario: Subtle identity question (edge case)

### Broker Reply (send this)
```
Subject: RE: 1800 Broad St

Thanks for reaching out about 1800 Broad.

Before I send over the details, it would help to know a bit more
about what you're looking for - is this for a specific tenant you're
working with, or more of a general search? Just want to make sure
I'm sending relevant info.

Mike
```
**No attachment**

### Expected Behavior
| Item | Expected |
|------|----------|
| Events | `needs_user_input:confidential` |
| AI Response | **NULL** (should not auto-reply) |
| Notification | `action_needed` with reason confidential/client_question |
| Auto-Reply Sent | NO |

### Actual Results
| Item | Actual |
|------|--------|
| Events Detected | |
| AI Response | |
| Notification Created | |
| Auto-Reply Sent | Yes / No |

---

### _(User composes reply in UI)_

### User Reply Sent
| Item | Actual |
|------|--------|
| Content | _(paste what you sent)_ |
| Time Sent | |

---

### _(Wait to see if follow-up resumes after broker silence)_

### Follow-up Resumed?
| Item | Actual |
|------|--------|
| Follow-up Sent | Yes / No |
| Time | |
| Content | _(paste)_ |

---

### Broker Final Reply (send after user reply or follow-up)
```
Subject: RE: 1800 Broad St

No problem, I understand. Here's what we've got:

1800 Broad is 35,000 SF, asking $9/SF NNN. 26' clear height,
4 dock doors and 1 drive-in. Power is 400A 3-phase.

Flyer attached.
```
**Attach**: PDF

### Expected Final
| Item | Expected |
|------|----------|
| Data Extracted | All fields |
| Flyer Link | Populated |
| AI Response | Closing email |
| Events | `close_conversation` |
| End State | COMPLETE |

### Actual Final
| Item | Actual |
|------|--------|
| Data Extracted | |
| AI Response | _(paste)_ |
| End State | |

---

# Property 5: 2525 Center West Pkwy

## Scenario: Full data + tour request + optional field test

**KEY TEST:** Has all required fields + Rail, missing Office % (optional) → should NOT block tour/close

### Broker Reply (send this)
```
Subject: RE: 2525 Center West Pkwy

2525 Center West is a great space - 38,000 SF, $7.25 NNN,
32' clear with 6 docks and 2 drive-ins. Power is 480V 3-phase.
Building has rail access - Norfolk Southern siding on the east end.

I'm actually going to be at the property Thursday around 2pm
if your client wants to take a quick walk through. No pressure
either way - just let me know.

Flyer attached.
```
**Attach**: PDF

### Expected Behavior
| Item | Expected |
|------|----------|
| Standard Fields | ALL complete: SF: 38000, Ops Ex: 7.25, Ceiling: 32, Docks: 6, Drive Ins: 2, Power: 480V 3-phase |
| Flyer Link | Populated |
| **Rail Access** | "Norfolk Southern siding" ✅ (required - complete) |
| **Office %** | ❌ MISSING (but optional - should NOT block) |
| Events | `tour_requested` (should detect tour offer) |
| AI Response | **NULL** (user approves tour response) |
| Notification | `action_needed` with tour details |

### ⚠️ KEY TEST: Ask Optional Doesn't Block
- Office % is "ask_optional" - missing should NOT block tour flow or closing
- All required fields ARE provided (including Rail Access)

### Actual Results
| Item | Actual |
|------|--------|
| Data Extracted | |
| Flyer Link | |
| Rail Access | |
| Office % | |
| Events Detected | |
| AI Response | |
| Notification Created | |

### Notes
```
(Did missing Office % block anything? It shouldn't.)
```

---

### _(User handles tour request in UI)_

### User Action
| Item | Actual |
|------|--------|
| Tour Response Sent | |
| Content | _(paste)_ |

---

### After Tour Handled
| Item | Expected | Actual |
|------|----------|--------|
| Row Status | Should be COMPLETE (all data was in first email) | |
| Closing Email | May or may not need one | |

---

# Property 6: 2017 St. Josephs Drive

## Scenario: Building Condition Notes (note mode) + identity question

**KEY TEST:** Building condition mentions should auto-append to "Building Condition Notes" column (note mode)

### Broker Reply (send this)
```
Subject: RE: 2017 St. Josephs Drive

2017 St. Josephs is 18,500 SF. The building was renovated in 2021 -
new roof, updated electrical, and the loading docks were completely
rebuilt. Previous tenant kept it in great shape.

Just a heads up - the HVAC system is original from 1998 and might
need attention in the next few years.

Who's this for by the way? Just want to make sure there aren't
any conflicts on our end.
```
**No attachment**

### Expected Behavior
| Item | Expected |
|------|----------|
| Data Extracted | Total SF: 18500 |
| **Building Condition Notes** | Should contain: "Renovated 2021, new roof, updated electrical, rebuilt docks. HVAC original 1998 may need attention" (AUTO-APPENDED, not asked) |
| Events | `needs_user_input:confidential` |
| AI Response | **NULL** (escalation for identity question) |
| Notifications | `action_needed:needs_user_input:confidential` |

### ⚠️ KEY TEST: Note Mode
- AI should NOT ask "what's the building condition?"
- AI should AUTOMATICALLY append the building condition info to that column
- This tests the "note" column mode behavior

### Actual Results
| Item | Actual |
|------|--------|
| Data Extracted | |
| **Building Condition Notes populated?** | Yes / No |
| Building Condition Notes content | |
| Events Detected | |
| AI Response | |
| Notifications Created | |

### Notes
```
(Did note mode work? Was building condition auto-appended without asking?)
```

---

### _(User handles escalation in UI)_

### User Reply
| Item | Actual |
|------|--------|
| Content | _(paste)_ |

---

### Broker Final Reply (send this)
```
Subject: RE: 2017 St. Josephs Drive

Thanks for clarifying. Here's the rest of the details:

NNN is $2.75/SF. 20' clear height. 2 dock doors, 1 drive-in.
Power is 200A 3-phase.

Brochure attached.
```
**Attach**: PDF

### Expected Final
| Item | Expected |
|------|----------|
| Data Extracted | Remaining fields |
| AI Response | Closing email |
| End State | COMPLETE |

### Actual Final
| Item | Actual |
|------|--------|
| Data Extracted | |
| AI Response | _(paste)_ |
| End State | |

---

# Property 7: 9300 Lottsford Rd

## Scenario: All info but URL instead of PDF attachment

### Broker Reply (send this)
```
Subject: RE: 9300 Lottsford Rd

Here's the rundown on 9300 Lottsford:
- 42,000 SF
- $6.75/SF NNN
- 30' clear
- 8 dock doors, 2 drive-ins
- 400A 3-phase

Full brochure is on our website:
https://example.com/listings/9300-lottsford

Let me know if you have questions.
```
**NO attachment**

### Expected Behavior
| Item | Expected |
|------|----------|
| Data Extracted | All numeric fields |
| Flyer Link | Should extract URL to Flyer/Link column |
| AI Response | Closing email (if URL counts as flyer) OR request flyer attachment |
| End State | COMPLETE (if URL accepted) or waiting for flyer |

### Actual Results
| Item | Actual |
|------|--------|
| Data Extracted | |
| Flyer Link | |
| AI Response | _(paste)_ |
| Events Detected | |
| End State | |

### Notes
```
(Did URL count as flyer? Did it close or request attachment?)
```

---

# Final Summary

## Campaign End State

| Property | Expected End State | Actual End State | Match? |
|----------|-------------------|------------------|--------|
| 699 Industrial Park Dr | COMPLETE | | |
| 1 Randolph Ct | COMPLETE | | |
| 150 Trade Center Court | NON-VIABLE | | |
| 135 Trade Center Court | COMPLETE | | |
| 1800 Broad St | COMPLETE | | |
| 2525 Center West Pkwy | COMPLETE | | |
| 2017 St. Josephs Drive | COMPLETE | | |
| 9300 Lottsford Rd | COMPLETE | | |

## Notifications Created

| Property | Expected Notification | Actual Notification | Match? |
|----------|----------------------|---------------------|--------|
| 699 Industrial | row_completed | | |
| 1 Randolph | row_completed | | |
| 150 Trade Center | property_unavailable | | |
| 135 Trade Center | row_completed | | |
| 1800 Broad | action_needed (confidential) → row_completed | | |
| 2525 Center West | action_needed (tour) → row_completed | | |
| 2017 St. Josephs | action_needed (issue + confidential) → row_completed | | |
| 9300 Lottsford | row_completed | | |

## Edge Cases Tested

### Standard Behavior
| Edge Case | Property | Expected | Actual | Pass/Fail |
|-----------|----------|----------|--------|-----------|
| Messy formatting extraction | 699 | Extract all | | |
| Different signer (don't update contact) | 699 | Contact unchanged | | |
| Multi-turn accumulation | 1 Randolph | Accumulate correctly | | |
| Follow-up resumes after partial | 1 Randolph | FU sent | | |
| Vague new property suggestion | 150 | Detect intent | | |
| Subtle identity question | 1800 | Escalate | | |
| Data + tour in same email | 2525 | Both detected | | |
| URL as flyer (not attachment) | 9300 | Accept or request? | | |

### ⭐ Custom Field Behavior (ALL 3 UI MODES)
| Edge Case | Property | Expected | Actual | Pass/Fail |
|-----------|----------|----------|--------|-----------|
| **ask_required blocks closing** | 1 Randolph | Reply #3: AI asks for Rail Access, does NOT close | | |
| **ask_required allows closing when provided** | 1 Randolph | Reply #4: AI closes after Rail provided | | |
| **ask_required extracted correctly** | 699 | Rail Access = "CSX spur" | | |
| **ask_optional extracted** | 699 | Office % = 15 | | |
| **ask_optional doesn't block** | 2525 | Missing Office % doesn't prevent tour/close | | |
| **note auto-appends** | 2017 | Building Condition Notes populated automatically | | |
| **note never asked** | 2017 | AI didn't ask "what's the building condition?" | | |
| **note auto-appends** | 699 | Building Condition Notes = "roof, HVAC" | | |

---

## Firestore Document Tracking

### Campaign Start (After Outreach Sent)

| Collection | Expected Count | Actual Count | Document IDs |
|------------|----------------|--------------|--------------|
| threads | 7 (one per property) | | |
| msgIndex | 7+ (one per sent email) | | |
| convIndex | 7 (one per conversation) | | |
| outbox | 0 (all processed) | | |
| notifications | 0 (no events yet) | | |

### After Each Reply Processed

**Reply 1 (699 Industrial - Complete Info)**
| Collection | Change | Details |
|------------|--------|---------|
| threads | Updated | `followUpStatus: completed` |
| msgIndex | +1 | Broker reply indexed |
| notifications | +1 | `row_completed` created |

**Reply 2 (1 Randolph - Partial #1)**
| Collection | Change | Details |
|------------|--------|---------|
| threads | Updated | Data accumulated, followUpStatus: scheduled |
| msgIndex | +1 | |
| notifications | +1 | `sheet_update` |

**Reply 3 (150 Trade Center - Unavailable)**
| Collection | Change | Details |
|------------|--------|---------|
| threads | Updated | `followUpStatus: stopped` |
| msgIndex | +1 | |
| notifications | +1 | `property_unavailable` |

**Reply 4 (1800 Broad - Identity Question)**
| Collection | Change | Details |
|------------|--------|---------|
| threads | Updated | `followUpStatus: paused` |
| msgIndex | +1 | |
| notifications | +1 | `action_needed:needs_user_input:confidential` |

_(Continue tracking for each reply...)_

### Follow-Up Timing Log

| Property | Outreach Sent | FU #1 Expected | FU #1 Actual | FU #2 Expected | FU #2 Actual |
|----------|---------------|----------------|--------------|----------------|--------------|
| 699 Industrial | | N/A (complete) | | | |
| 1 Randolph | | +5m from reply | | +10m from FU1 | |
| 150 Trade Center | | N/A (unavail) | | | |
| 1800 Broad | | +5m from user reply | | | |
| 2525 Center West | | N/A (tour escalate) | | | |
| 2017 St. Josephs | | +5m from user reply | | | |
| 9300 Lottsford | | +5m if no reply | | | |

## Issues Found
```
1.

2.

3.

```

## Phrase Variation Check
_Did the AI use varied language across responses? Note any repetition:_
```


```

## Overall Assessment
```


```

---

# Scheduler Run Log

Track every scheduler run during the test.

| Run ID | Time | Triggered By | What Processed | Notes |
|--------|------|--------------|----------------|-------|
| | | schedule/manual | | |
| | | | | |
| | | | | |
| | | | | |
| | | | | |
| | | | | |
| | | | | |
| | | | | |

---

# Final Log Review

_Run `python3 scripts/e2e_tools.py review-logs` at the end_

## Errors Found
```


```

## Warnings Found
```


```

## Unnecessary Operations
_(Any duplicate processing, wasted API calls, etc.)_
```


```

## Cleanup Needed
_(Stale documents, orphaned data, etc.)_
```


```

## Bugs/Issues to Fix
```
1.

2.

3.
```

---

_Last Updated: _______________
