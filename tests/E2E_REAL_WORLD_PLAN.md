# ⚠️ DEPRECATED - Use E2E_TEST_RUN_SHEET.md Instead

**This file is outdated.** The current E2E test plan is at:
- `/E2E_TEST_RUN_SHEET.md` - Run sheet with expected vs actual
- `/tests/E2E_REPLY_CHEATSHEET.md` - Copy-paste broker replies

---

# E2E Real World Test Plan (OLD - March 9, 2026)

**Date:** March 9, 2026
**Test File:** `test_pdfs/E2E_Real_World_Test.xlsx`
**Follow-up Config:** 3 follow-ups at **1 hour**, **2 hours**, **3 hours**

---

## BUG FIXES TO VERIFY (March 9, 2026)

| Fix | How to Verify |
|-----|---------------|
| **Custom required fields checked** | Reply 7 (2525 partial): AI should ask for Parking, NOT close conversation |
| **[NAME] replaced in tour request** | Reply 3 (tour): AI suggested email should say "Hi Brian," not "Hi [NAME]," |
| **PDF links to new property row** | Reply 2: After approving 135 Trade Center, PDF link should appear in new row |
| **AI closes when required fields complete** | Reply 1, 6, 8: Should send closing email, NOT ask for optional fields |
| **Pending reply: right side + signature** | After any AI reply queued, check conversation panel |
| **Status column first with chevron** | Check table layout immediately after campaign start |
| **Action count badge (not total)** | Badge should show action_needed count only |
| **Completed Campaigns header stat** | Should increment when row_completed fires |
| **Full name to sheet, first name in email** | Reply 2 new property: sheet gets full name, email says "Hi [FirstName]," |
| **Unavailable reason to comments** | Reply 2: Check "Listing Brokers Comments" column |
| **InlineReplyComposer works** | Reply 5: Click "Input Needed" → composer appears in panel |
| **InlineNewPropertyCard works** | Reply 2: Card appears in conversation, can approve |

---

## CUSTOM FIELD MODES TEST

This E2E tests all 3 custom field configuration modes:

| Column | Mode | Behavior |
|--------|------|----------|
| **Parking Spaces** | `ask_required` | Must have value before closing, AI will ask if missing |
| **Yard Space** | `ask_optional` | AI will ask if missing but not required for closing |
| **Environmental Notes** | `note` | Append any mentions, never ask for it |

### Excel File Setup
Add these 3 columns after the standard fields:
- Column S: `Parking Spaces`
- Column T: `Yard Space`
- Column U: `Environmental Notes`

### Column Mapping Configuration
During campaign setup, configure:
1. **Parking Spaces** → "Ask (Required)"
2. **Yard Space** → "Ask (Optional)"
3. **Environmental Notes** → "Note"

### Initial Outreach Script
The Excel file (`E2E_Real_World_Test.xlsx`) now includes a **Script** sheet with the outreach template.
The script asks for parking upfront: `> How many parking spaces are available?`

**IMPORTANT:** The Script sheet is included in the Excel file - no manual script entry needed.

---

## MONITORING TOOLS

```bash
# Take snapshots before/after each phase
python3 tests/e2e_monitor.py snapshot before
python3 tests/e2e_monitor.py snapshot after_initial
python3 tests/e2e_monitor.py diff

# Check current state
python3 tests/e2e_monitor.py firebase
python3 tests/e2e_monitor.py outlook
```

---

## Properties Overview

| Row | Property | Contact | Email | Scenario |
|-----|----------|---------|-------|----------|
| 3 | 699 Industrial Park Dr | Jeff Wilson | bp21harrison | Complete + All Custom Fields |
| 4 | 150 Trade Center Court | Luke Coffey | bp21harrison | Unavailable + New Property (→ 135 Trade Center, matches PDF) |
| 5 | 2017 St. Josephs Drive | Brian Greene | manifold | Tour Request |
| 6 | 9300 Lottsford Rd | Craig Cheney | manifold | Complete + Parking + Env Notes |
| 7 | 1 Randolph Ct | Scott Atkins | bp21harrison | Identity Question |
| 8 | 1800 Broad St | Marcus Thompson | bp21harrison | Complete + PDF + All Custom |
| 9 | 2525 Center West Pkwy | Lisa Anderson | manifold | Partial → Multi-turn (test custom required fields) |

---

## Phase 1: Campaign Setup

### User Actions
1. Create new client, upload `test_pdfs/E2E_Real_World_Test.xlsx`
2. **Column Mapping:**
   - Standard fields (Total SF, Docks, etc.) → "Ask"
   - **Parking Spaces** → "Ask (Required)"
   - **Yard Space** → "Ask (Optional)"
   - **Environmental Notes** → "Note"
3. Configure: **3 follow-ups** at **1 hour**, **2 hours**, **3 hours**
4. Start campaign
5. Tell Claude "campaign started"

### VERIFY UI IMMEDIATELY:
- [ ] **Status column is FIRST** (chevron + status button together)
- [ ] **Header stats:** "Completed Campaigns" | "Properties Completed" | "Sheet Updates"
- [ ] **Your Clients stats:** "Active Clients" | "Actions Needed"
- [ ] **Pending section** in conversation panel shows 7 emails
- [ ] **Expand a pending email** → signature visible at bottom
- [ ] **Notification badge** next to chevron is subtle label style (not button)

---

## Phase 2: Send Broker Replies

### Reply 1: 699 Industrial Park Dr → COMPLETE + ALL CUSTOM FIELDS
**From:** bp21harrison@gmail.com
**Attach:** `699 Industrial Park Drive - Property Flyer.pdf` + `699 Industrial Park Drive - Floor Plan.pdf`

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
- Fenced yard area approximately 15,000 SF
- Phase 1 environmental completed, no issues found

See attached flyer and floor plan.

Jeff Wilson
```

**Expected:**
- All fields extracted
- **Parking Spaces:** 75 ✅
- **Yard Space:** 15000 (or "15,000 SF") ✅
- **Environmental Notes:** "Phase 1 completed, no issues found" ✅
- Closing email sent

**VERIFY BUG FIXES:**
- [ ] **Closing email says:** "Thanks for all the details... I have everything I need..."
- [ ] **AI does NOT ask for:** Rent, Flyer, or any optional fields not in required list
- [ ] **Thread status:** "Completed" (green badge)
- [ ] **row_completed notification** appears inline in conversation
- [ ] **Completed Campaigns stat** increments in header
- [ ] **Pending reply** (if visible before send) shows on RIGHT side with signature

---

### Reply 2: 150 Trade Center Court → UNAVAILABLE + NEW PROPERTY
**From:** bp21harrison@gmail.com
**Attach:** `135 Trade Center Court - Brochure.pdf` (this PDF is for the NEW property 135 Trade Center)

```
Hi Jill,

Sorry for the delay - 150 Trade Center Court just went under contract last week.

However, I have another property at Gun Club Industrial Park - 135 Trade Center Court. It's 7,500 SF at $15/SF NNN with ample parking. Attached is the brochure.

Let me know if interested.

Luke Coffey
```

**Expected:**
- `property_unavailable` → 150 Trade Center moved to NON-VIABLE
- **Thank-you reply sent automatically** in current thread: "Thanks for the update—understood on 150 Trade Center being unavailable..."
- `action_needed` for new property approval (135 Trade Center)

**VERIFY NEW PROPERTY FLOW:**
- [ ] **Thank-you reply sent** in 150 Trade Center conversation: "I'll send a separate email to keep things organized"
- [ ] **Listing Brokers Comments column:** Contains "under contract" or similar reason
- [ ] **InlineNewPropertyCard** appears in conversation panel (not a modal!)
- [ ] **Card shows:** 135 Trade Center Court details (extracted from PDF)
- [ ] **PDF extraction from brochure should show:**
  - Building D: 7,500 SF available
  - Rate: $15.00/SF/YR NNN
  - Contact: Luke Coffey
- [ ] **Status column:** Shows "New Property" button
- [ ] **Click "Approve & Send":**
  - [ ] New row created in sheet for 135 Trade Center
  - [ ] **PDF link appears in Flyer/Link column of NEW row** (not the non-viable row)
  - [ ] Leasing Contact column: Full name (e.g., "Luke Coffey")
  - [ ] Email greeting: First name only ("Hi Luke,")
  - [ ] **NEW conversation thread created** with subject "135 Trade Center Court, Augusta, GA" (NOT a reply to 150)
  - [ ] Pending email appears in conversation panel

---

### Reply 3: 2017 St. Josephs Drive → TOUR REQUEST
**From:** baylor@manifoldengineering.ai

```
Hi Jill,

Thanks for your persistence! I'd love to show you 2017 St. Josephs Drive.

Are you available Thursday or Friday afternoon for a tour?

Brian Greene
```

**Expected:** `tour_requested`, thread paused, blue highlight

**VERIFY BUG FIXES:**
- [ ] **[NAME] replaced:** AI suggested reply says "Hi Brian," (NOT "Hi [NAME],")
- [ ] **InlineReplyComposer** appears at bottom of conversation
- [ ] **Placeholder text** in empty input should NOT show [NAME]

---

### Reply 4: 9300 Lottsford Rd → COMPLETE + PARKING + ENV NOTES (NO YARD)
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

Note: Previous tenant was a dry cleaner but all environmental remediation was completed in 2024.

Craig Cheney
```

**Expected:**
- **Parking Spaces:** 45 ✅
- **Yard Space:** (empty - not mentioned, AI should ask since it's ask_optional)
- **Environmental Notes:** "Previous tenant dry cleaner, remediation completed 2024" ✅
- Thread stays `active` (AI asks about yard space)

---

### Reply 5: 1 Randolph Ct → IDENTITY QUESTION
**From:** bp21harrison@gmail.com

```
Hi Jill,

Before I share details on 1 Randolph Ct, I need to know who your client is. Our ownership requires this.

Who are you representing?

Scott Atkins
```

**Expected:** `needs_user_input:confidential`, thread paused, blue highlight

**VERIFY BUG FIXES:**
- [ ] **Thread status:** "Paused" (orange badge)
- [ ] **InlineReplyComposer** appears at bottom of thread (not a modal!)
- [ ] **Status column:** Shows "Input Needed" button
- [ ] **Action count badge:** Shows count (should be at least 1)
- [ ] **Click "Input Needed" button:**
  - [ ] Conversation panel expands
  - [ ] Auto-scrolls to InlineReplyComposer
- [ ] **Compose and send reply:**
  - [ ] Panel stays open (doesn't collapse)
  - [ ] Pending reply appears in thread (right side + signature)

---

### Reply 6: 1800 Broad St → COMPLETE + ALL CUSTOM FIELDS
**From:** bp21harrison@gmail.com
**Attach:** `1800 Broad Street - Property Flyer.pdf` + `1800 Broad Street - Floor Plan.pdf`

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
- Large secured yard: 20,000 SF
- Clean environmental history

Flyer and floor plan attached.

Marcus Thompson
```

**Expected:**
- All fields extracted
- **Parking Spaces:** 95 ✅
- **Yard Space:** 20000 ✅
- **Environmental Notes:** "Clean environmental history" ✅
- Closing email sent

---

### Reply 7: 2525 Center West Pkwy → PARTIAL (Test Custom Required Fields)
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
- Standard fields extracted (6 fields)
- **Parking missing** (ask_required) → AI MUST ask for parking
- **Yard missing** (ask_optional) → AI may ask
- Thread stays `active` (NOT completed)

**VERIFY BUG FIX (Custom Required Fields):**
- [ ] **AI does NOT send closing email** - parking is required but missing
- [ ] **AI reply asks for parking count**
- [ ] **Thread status:** Active (yellow badge), NOT Completed
- [ ] **Row NOT marked as completed** in sheet

---

## Phase 3: Multi-turn Completion (2525 Center West)

### Reply 8: Add Parking + Yard
**From:** baylor@manifoldengineering.ai
**Reply to:** AI's follow-up

```
Hi Jill,

Parking is 60 spaces. No designated yard area at this property.

Lisa
```

**Expected:**
- **Parking Spaces:** 60 ✅
- **Yard Space:** "None" or similar ✅
- All required fields complete → Closing email sent

---

## Expected Final Results

| Row | Property | Status | Parking | Yard | Env Notes |
|-----|----------|--------|---------|------|-----------|
| 3 | 699 Industrial Park | completed | 75 | 15000 | Phase 1 completed |
| 4 | 150 Trade Center | NON-VIABLE | - | - | - |
| NEW | 135 Trade Center | active (new) | - | - | - |
| 5 | 2017 St. Josephs | paused | - | - | - |
| 6 | 9300 Lottsford | active→completed | 45 | (asked) | Remediation 2024 |
| 7 | 1 Randolph Ct | paused | - | - | - |
| 8 | 1800 Broad St | completed | 95 | 20000 | Clean history |
| 9 | 2525 Center West | completed | 60 | None | - |

---

## Success Checklist

### Bug Fixes (March 10, 2026)
- [ ] **Custom required fields:** Parking (ask_required) blocks closing - Reply 7 should NOT close
- [ ] **[NAME] replaced:** Tour request email shows "Hi Brian," not "Hi [NAME],"
- [ ] **PDF links to new row:** 135 Trade Center row gets PDF link, NOT the 150 row
- [ ] **AI closes when complete:** Sends closing email, does NOT ask for optional fields
- [ ] **Pending reply positioning:** Right side, shows signature
- [ ] **Status column first:** Chevron + button in first column
- [ ] **Action count badge:** Shows action_needed count only (subtle label style)
- [ ] **Completed Campaigns stat:** Increments correctly in header
- [ ] **Full name to sheet:** New property gets full contact name in sheet
- [ ] **First name in email:** Greeting uses first name only
- [ ] **Unavailable reason:** Written to Listing Brokers Comments column
- [ ] **InlineReplyComposer:** Works in conversation panel (not modal)
- [ ] **InlineNewPropertyCard:** Works in conversation panel (not modal)
- [ ] **Panel stays open:** After sending, doesn't collapse

### Custom Field Modes
- [ ] **ask_required (Parking):** AI asks when missing, blocks closing until provided
- [ ] **ask_optional (Yard):** AI asks when missing, does NOT block closing
- [ ] **note (Environmental):** AI appends mentions, never asks for it

### Core Features
- [ ] PDF extraction (flyer vs floorplan separation)
- [ ] NON-VIABLE detection ("under contract")
- [ ] Escalations (tour, identity question)
- [ ] Multi-turn conversations
- [ ] Blue highlighting for paused rows
- [ ] Row completion notifications

---

## Quick Reference: Broker Inboxes

**bp21harrison@gmail.com (4):** Rows 3, 4, 7, 8
**baylor@manifoldengineering.ai (3):** Rows 5, 6, 9
