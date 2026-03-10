# E2E Test Plan - Complete Campaign to Completion

## Test Objectives
1. **Follow-up system**: Test mid-conversation follow-up when broker doesn't respond
2. **Flyer required**: Verify conversations don't close without a flyer link
3. **New property PDF handling**: Ensure PDFs go to new property row, not old unavailable row
4. **Full completion**: All rows end as either "complete" or "non-viable"

---

## Configuration Instructions

### 1. Create New Client
- **Name**: `E2E Test Client - March 2026`
- **From Email**: `baylor.freelance@outlook.com`

### 2. Follow-Up Settings
| Setting | Value |
|---------|-------|
| Wait Time | 2 |
| Wait Unit | **Minutes** |
| Max Follow-ups | 3 |

### 3. Upload Excel with These Properties

| Property Address | City | Leasing Contact | Email |
|------------------|------|-----------------|-------|
| 100 Commerce Blvd | Augusta | John Smith | bp21harrison@gmail.com |
| 200 Industrial Way | Augusta | Sarah Jones | bp21harrison@gmail.com |
| 300 Warehouse Dr | Augusta | Mike Brown | bp21harrison@gmail.com |
| 400 Distribution Ct | Augusta | Lisa White | bp21harrison@gmail.com |

### 4. Column Configuration (IMPORTANT)
During the column mapping step, configure these columns:

**Required Fields (set mode to "Ask Required"):**
- Total SF
- Ops Ex /SF
- Drive Ins
- Docks
- Ceiling Ht
- Power
- **Flyer / Link** ← Set this to "Ask (Required)" to test flyer requirement

**Identity Fields (auto-mapped, don't change):**
- Property Address, City, Email, Leasing Contact

**Optional/Notes:**
- Listing Brokers Comments (set to "Note" mode)
- Rent/SF /Yr (set to "Accept Only" - never request)
- Gross Rent (set to "Skip" - formula column)

---

## Test Scenarios & Broker Reply Scripts

### Property 1: 100 Commerce Blvd - Complete Info with Flyer
**Goal**: Broker provides all info + flyer in one reply. Should close immediately.

**Broker Reply (copy-paste to bp21harrison@gmail.com):**
```
Subject: RE: 100 Commerce Blvd - Augusta Property Inquiry

Hi,

Happy to help with 100 Commerce Blvd. Here are the details:

- Total SF: 45,000
- NNN: $2.25/SF/yr
- Drive-in doors: 2
- Dock doors: 4
- Clear height: 28 feet
- Power: 400A 3-phase

I've attached the property flyer for your review.

Best,
John Smith
ABC Commercial Real Estate
```

**Attach**: Any PDF file (will be uploaded to Drive)

**Expected Result**:
- All fields extracted and written to sheet
- Flyer link populated from PDF upload
- Closing email sent: "Thank you for the comprehensive information..."
- Row marked complete

---

### Property 2: 200 Industrial Way - Partial Info, Needs Follow-up
**Goal**: Broker provides partial info. System should request remaining fields. Then we DON'T reply for 2+ minutes to trigger follow-up.

**Broker Reply #1 (copy-paste):**
```
Subject: RE: 200 Industrial Way - Augusta Property Inquiry

Hi,

The space at 200 Industrial Way is 32,000 SF with 24' clear height.

Let me know if you need anything else.

Sarah
```

**Expected After Reply #1**:
- AI extracts: Total SF = 32000, Ceiling Ht = 24
- AI requests: Ops Ex, Drive Ins, Docks, Power, Flyer
- **DO NOT REPLY** - wait 2+ minutes for follow-up

**Expected After ~2 Minutes**:
- System sends follow-up email asking for remaining info

**Broker Reply #2 (after follow-up arrives, copy-paste):**
```
Subject: RE: 200 Industrial Way - Augusta Property Inquiry

Sorry for the delay! Here's the rest:

- NNN/CAM: $1.85/SF
- 1 drive-in door
- 3 dock doors
- Electric: 200A single-phase

Flyer attached.

Sarah
```

**Attach**: Any PDF file

**Expected Final Result**:
- All fields complete
- Flyer link populated
- Closing email sent
- Row marked complete

---

### Property 3: 300 Warehouse Dr - Unavailable + New Property Suggestion
**Goal**: Property unavailable, broker suggests alternative with PDF. PDF should go to NEW row, not old row.

**Broker Reply (copy-paste):**
```
Subject: RE: 300 Warehouse Dr - Augusta Property Inquiry

Hi,

Unfortunately 300 Warehouse Dr just went under contract last week.

However, I have a great alternative - 350 Logistics Lane in Augusta. It's:
- 55,000 SF
- 30' clear
- 6 dock doors, 2 drive-ins
- 480V 3-phase power
- NNN is $2.50/SF

I've attached the flyer for 350 Logistics Lane.

Mike Brown
```

**Attach**: PDF file (this is the key test - should go to NEW row)

**Expected Result**:
- Original row (300 Warehouse Dr) moved to NON-VIABLE section
- NO flyer link written to 300 Warehouse Dr
- Notification created: "new_property_pending_approval" with PDF manifest
- When approved: NEW row created for "350 Logistics Lane"
- PDF link written to NEW row (350 Logistics Lane)
- All extracted data written to new row
- If all fields present, row should be complete

---

### Property 4: 400 Distribution Ct - Needs User Input (Identity Question)
**Goal**: Broker asks confidential question, system escalates.

**Broker Reply (copy-paste):**
```
Subject: RE: 400 Distribution Ct - Augusta Property Inquiry

Hi,

Thanks for reaching out about 400 Distribution Ct.

Before I share details, can you tell me who your client is? I need to make sure we don't have any conflicts with existing prospects.

Thanks,
Lisa White
```

**Expected Result**:
- `needs_user_input:confidential` event detected
- Notification created for user action
- Auto-reply NOT sent (conversation paused)
- Row stays active (not complete, not non-viable)

**User Action**: Use UI to compose reply (suggest: "My client prefers to remain confidential at this stage...")

**After User Sends Reply & Broker Responds with Info:**
```
Subject: RE: 400 Distribution Ct - Augusta Property Inquiry

I understand. Here are the property details:

- 28,000 SF available
- $1.95/SF NNN
- 2 drive-ins, 4 docks
- 26' clear height
- 400A 3-phase

Attached is our marketing brochure.

Lisa
```

**Attach**: PDF file

**Expected Final Result**:
- All fields extracted
- Flyer link populated
- Row marked complete

---

## Execution Checklist

### Pre-Test
- [ ] Outlook (baylor.freelance) cleared
- [ ] Previous test client deleted
- [ ] bp21harrison@gmail.com inbox cleared

### Campaign Launch
- [ ] Create client with settings above
- [ ] Upload Excel with 4 properties
- [ ] Configure column mappings
- [ ] Start campaign
- [ ] Verify 4 outreach emails sent

### Property 1 (100 Commerce Blvd)
- [ ] Send broker reply with complete info + PDF
- [ ] Wait for system processing (~2-3 min)
- [ ] Verify all fields extracted
- [ ] Verify flyer link populated
- [ ] Verify closing email sent
- [ ] Verify row shows as complete

### Property 2 (200 Industrial Way)
- [ ] Send broker reply #1 (partial info, no PDF)
- [ ] Verify partial extraction
- [ ] Verify AI requests remaining fields
- [ ] **Wait 2+ minutes WITHOUT replying**
- [ ] Verify follow-up email sent
- [ ] Send broker reply #2 with remaining info + PDF
- [ ] Verify all fields complete
- [ ] Verify row complete

### Property 3 (300 Warehouse Dr)
- [ ] Send broker reply (unavailable + new property + PDF)
- [ ] Verify 300 Warehouse Dr moved to NON-VIABLE
- [ ] Verify NO flyer link on 300 Warehouse Dr
- [ ] Verify notification for new property approval
- [ ] Click approve in UI
- [ ] Verify new row "350 Logistics Lane" created
- [ ] Verify PDF link on NEW row
- [ ] Verify extracted data on new row
- [ ] If complete, verify closing email sent

### Property 4 (400 Distribution Ct)
- [ ] Send broker reply (identity question)
- [ ] Verify escalation notification
- [ ] Verify NO auto-reply sent
- [ ] Use UI to compose and send reply
- [ ] Send final broker reply with all info + PDF
- [ ] Verify row complete

### Final Verification
- [ ] All original 4 rows: complete or non-viable
- [ ] New property row (350 Logistics Lane): complete
- [ ] Stats cards show correct values
- [ ] Notifications reflect all activity

---

## Gmail Setup (bp21harrison@gmail.com)

Before starting, clear the inbox. Then for each reply:

1. Open Gmail
2. Find the outreach email for the property
3. Click Reply
4. Paste the script above
5. Attach PDF if indicated
6. Send

---

## Timing Guide

| Action | Timing |
|--------|--------|
| Launch campaign | T+0 |
| Reply to Property 1 | T+1 min (after outreach arrives) |
| Reply to Property 2 (partial) | T+3 min |
| DO NOT REPLY - wait for follow-up | T+5 min (follow-up should arrive) |
| Reply to Property 2 (complete) | T+6 min |
| Reply to Property 3 | T+8 min |
| Reply to Property 4 | T+10 min |
| Use UI to respond to Property 4 escalation | T+12 min |
| Final broker reply for Property 4 | T+15 min |

---

## Success Criteria

1. **100 Commerce Blvd**: Complete in 1 exchange
2. **200 Industrial Way**: Complete after follow-up triggered mid-conversation
3. **300 Warehouse Dr**: NON-VIABLE, new property created with PDF on correct row
4. **400 Distribution Ct**: Complete after user handles escalation
5. **350 Logistics Lane** (new): Complete with PDF link populated
6. **All required fields including Flyer / Link** populated on complete rows
