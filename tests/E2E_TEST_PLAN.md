# E2E Test Plan - March 2026 (Updated)

**Date:** March 9, 2026
**Test File:** Create new Excel with 5 properties
**Follow-up Config:** 2 follow-ups, 2 minutes each (for quick testing)

---

## Recent Bug Fixes to Verify

| Fix | What to Test |
|-----|--------------|
| **AI closes when required fields complete** | Should send closing email, NOT ask for optional fields |
| **Pending reply shows right side + signature** | Reply queued should appear right-aligned with signature |
| **Status column first with chevron** | Table layout: Status → Name → Last Updated → Open |
| **Action count badge** | Shows action_needed count only, not total notifications |
| **Completed Campaigns header stat** | Shows count of clients with row_completed |
| **Full name to sheet, first name in email** | New property: sheet gets "Joe Smith", email says "Hi Joe," |
| **Unavailable reason to comments column** | Writes to "Listing Brokers Comments" column |

---

## Test Configuration

### Create Excel File with These Rows:

| Row | Property Address | City | Leasing Contact | Email |
|-----|------------------|------|-----------------|-------|
| 3 | 100 Complete Info Dr | Augusta | Tom Wilson | bp21harrison@gmail.com |
| 4 | 200 Partial Info Ln | Augusta | Sarah Miller | bp21harrison@gmail.com |
| 5 | 300 Unavailable Way | Augusta | Mike Chen | bp21harrison@gmail.com |
| 6 | 400 New Property Ct | Augusta | Brian Greene | bp21harrison@gmail.com |
| 7 | 500 Confidential Blvd | Augusta | James Roberts | bp21harrison@gmail.com |

### Campaign Settings:
- **Follow-ups:** Enabled
- **Count:** 2 follow-ups
- **Timing:** 2 minutes each

---

## Phase 1: Campaign Launch

### Actions:
1. Create new client with above Excel
2. Start campaign with follow-up settings

### Check Immediately:
- [ ] **UI:** Status column is FIRST column (chevron + button)
- [ ] **UI:** 5 pending emails in conversation panel
- [ ] **UI:** Can expand pending email to see content
- [ ] **UI:** Expanded pending shows email signature
- [ ] **UI:** "Completed Campaigns" stat in header
- [ ] **UI:** "Actions Needed" stat next to "Your Clients"

---

## Phase 2: Broker Replies

**Wait for emails to send, then send these replies from bp21harrison@gmail.com:**

---

### Reply 1: Complete Info (100 Complete Info Dr)

**Subject:** Re: 100 Complete Info Dr, Augusta

**Email Body:**
```
Hi Jill,

Happy to help with 100 Complete Info Dr. Here are the details:

- Total SF: 45,000
- Ops Ex/SF: $2.50 NNN
- Drive-ins: 2
- Docks: 4
- Ceiling Height: 28' clear
- Power: 2000 amps, 480V

Let me know if you need anything else.

Best,
Tom
```

**Expected AI Behavior:**
- Extracts all 6 required fields to sheet
- Sends closing email like: "Thanks for all the details... I have everything I need..."
- Emits `close_conversation` event with notes `all_info_gathered`
- **CRITICAL:** Does NOT ask for Rent, Flyer, or any other optional fields

**Check in UI:**
- [ ] Thread marked as "Completed" (green badge)
- [ ] `row_completed` notification appears inline
- [ ] No "Stop" button (conversation complete)
- [ ] Completed Campaigns stat increments

**Check in Sheet:**
- [ ] All 6 fields populated
- [ ] Row NOT moved (still viable)

---

### Reply 2: Partial Info (200 Partial Info Ln)

**Subject:** Re: 200 Partial Info Ln, Augusta

**Email Body:**
```
Hi Jill,

For 200 Partial Info Ln:
- Total SF: 32,000
- Docks: 3
- Ceiling: 24'

I'll get you the rest soon.

Thanks,
Sarah
```

**Expected AI Behavior:**
- Extracts Total SF, Docks, Ceiling Ht to sheet
- Sends reply asking ONLY for: Ops Ex/SF, Drive-ins, Power
- **CRITICAL:** Does NOT ask for Rent/SF, Gross Rent, or Flyer

**Check in UI:**
- [ ] Thread shows "Active" (yellow badge)
- [ ] "Awaiting Response" badge visible
- [ ] sheet_update notifications appear inline
- [ ] Pending AI reply visible (right-aligned with signature)

**Check in Sheet:**
- [ ] Total SF, Docks, Ceiling Ht populated
- [ ] Other required fields still empty

---

### Reply 2b: Complete Remaining (200 Partial Info Ln)

**Subject:** Re: 200 Partial Info Ln, Augusta

**Email Body:**
```
Hi Jill,

Here's the rest for 200 Partial Info Ln:
- Ops Ex: $1.85/SF
- Drive-ins: 1
- Power: 1200 amps

Sarah
```

**Expected AI Behavior:**
- Extracts remaining 3 fields
- Sends closing email
- Emits `close_conversation` with `all_info_gathered`

**Check in UI:**
- [ ] Thread now "Completed"
- [ ] `row_completed` notification appears

---

### Reply 3: Unavailable (300 Unavailable Way)

**Subject:** Re: 300 Unavailable Way, Augusta

**Email Body:**
```
Hi Jill,

Unfortunately 300 Unavailable Way just went under contract last week. We're expecting to close by end of month.

Do you want me to send you info on some other properties we have available?

Thanks,
Mike
```

**Expected AI Behavior:**
- Emits `property_unavailable` event
- Writes reason to "Listing Brokers Comments" column
- Row moves below NON-VIABLE divider
- May send reply asking about alternatives

**Check in UI:**
- [ ] `property_unavailable` notification appears inline

**Check in Sheet:**
- [ ] Row moved below NON-VIABLE divider
- [ ] "Listing Brokers Comments" column contains reason (e.g., "under contract")

---

### Reply 4: New Property (400 New Property Ct)

**Subject:** Re: 400 New Property Ct, Augusta

**Email Body:**
```
Hi Jill,

400 New Property Ct is no longer available, but I have a great alternative for you:

550 Better Option Dr in Augusta - 50,000 SF warehouse with 6 docks.
Contact Joe Smith at joe.smith@realestate.com for details.

Let me know if you'd like me to make an introduction.

Brian
```

**Expected AI Behavior:**
- Emits `property_unavailable` for 400 New Property Ct
- Emits `new_property` event with:
  - address: "550 Better Option Dr"
  - city: "Augusta"
  - email: "joe.smith@realestate.com"
  - contactName: "Joe Smith" (FULL NAME)

**Check in UI:**
- [ ] InlineNewPropertyCard appears in conversation
- [ ] Card shows property details
- [ ] "Approve & Send" and "Dismiss" buttons visible
- [ ] Status column shows "New Property" button

**Action:** Click "Approve & Send"

**Check After Approval:**
- [ ] New row in sheet for 550 Better Option Dr
- [ ] Leasing Contact column: "Joe Smith" (FULL NAME)
- [ ] Email greeting: "Hi Joe," (FIRST NAME ONLY)
- [ ] Pending email appears in conversation (right side, with signature)

---

### Reply 5: Confidential Question (500 Confidential Blvd)

**Subject:** Re: 500 Confidential Blvd, Augusta

**Email Body:**
```
Hi Jill,

Before I send over the details on 500 Confidential Blvd, can you tell me who your client is and what size they're looking for?

Thanks,
James
```

**Expected AI Behavior:**
- Emits `needs_user_input` with subreason `confidential`
- Does NOT send auto-reply
- Thread paused

**Check in UI:**
- [ ] Thread shows "Paused" (orange badge)
- [ ] InlineReplyComposer appears at bottom of thread
- [ ] Status column shows "Input Needed" button
- [ ] Action count badge shows count (not total notifications)

**Action:** Click "Input Needed" status button

**Check:**
- [ ] Conversation panel expands
- [ ] Auto-scrolls to InlineReplyComposer
- [ ] Can type and send reply

**Action:** Send a reply

**Check After Send:**
- [ ] Panel stays open (doesn't collapse)
- [ ] Pending reply appears in thread (right side)
- [ ] Pending reply shows signature

---

## Phase 3: UI Verification Summary

### Table Layout:
- [ ] Columns in order: Status (with chevron) | Name | Last Updated | Open

### Status Column:
- [ ] Chevron icon to left of status button
- [ ] Notification count badge is subtle (not button-styled)
- [ ] Badge shows action_needed count only

### Header Stats:
- [ ] First card: "Completed Campaigns" (count of clients with row_completed)
- [ ] Second card: "Properties Completed"
- [ ] Third card: "Sheet Updates"

### Your Clients Stats:
- [ ] First card: "Active Clients"
- [ ] Second card: "Actions Needed"

### Pending Messages:
- [ ] Display on RIGHT side (outbound style)
- [ ] Show email signature
- [ ] Have dashed purple border

---

## Success Criteria

### Backend (AI Processing):
- [ ] Only asks for REQUIRED fields (Total SF, Ops Ex, Drive-ins, Docks, Ceiling, Power)
- [ ] Sends closing email when required fields complete
- [ ] Emits `close_conversation` with "all_info_gathered"
- [ ] Does NOT ask for optional fields (Rent, Flyer, etc.)
- [ ] Writes unavailable reason to Listing Brokers Comments
- [ ] New property: full name to sheet, first name in email

### Frontend (UI):
- [ ] Status column is first with chevron
- [ ] Pending replies right-aligned with signature
- [ ] Action count shows action_needed only
- [ ] Completed Campaigns stat works
- [ ] Inline composers work in conversation panel
- [ ] Click action → expand + scroll to action
- [ ] Panel stays open after send

---

## Test Accounts

| Role | Email |
|------|-------|
| Outbound (Jill) | jill@mohrpartners.com |
| Broker replies | bp21harrison@gmail.com |
| Alt broker | baylor@manifoldengineering.ai |

---

## Quick Commands

```bash
# Check status
python tests/e2e_helpers.py status

# Trigger scheduler manually
python main.py

# Check threads
python tests/e2e_helpers.py threads
```
