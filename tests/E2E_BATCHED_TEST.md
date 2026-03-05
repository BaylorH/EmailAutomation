# E2E Batched Test Plan

This test is designed for minimal back-and-forth. You do batches of work, then hand off to me for verification.

---

## Pre-Test Setup (YOU DO)

### 1. Upload Excel File
- Use: `test_pdfs/E2E_Test_Augusta.xlsx`
- Create new client in dashboard, upload this file

### 2. Verify Excel Has These Properties

| Row | Property | City | Broker | Email |
|-----|----------|------|--------|-------|
| 3 | 100 Commerce Way | Augusta | Tom Wilson | bp21harrison@gmail.com |
| 4 | 200 Industrial Blvd | Evans | Sarah Miller | bp21harrison@gmail.com |
| 5 | 300 Warehouse Dr | Augusta | Mike Chen | bp21harrison@gmail.com |
| 6 | 400 Distribution Ave | Augusta | James Roberts | baylor@manifoldengineering.ai |
| 7 | 500 Logistics Ln | Evans | Karen Davis | baylor@manifoldengineering.ai |
| 8 | 600 Storage Ct | Augusta | Bob Thompson | baylor@manifoldengineering.ai |

### 3. Clean Slate
- Archive/delete old test emails from Outlook inbox
- Delete old notifications from Firestore if any remain

### 4. Start Campaign
- Click "Start Campaign" button
- Wait for confirmation that 6 emails queued

**Hand off to Claude: "Campaign started, 6 emails queued"**

---

## Checkpoint 1: Claude Verifies Initial State

I will check:
- [ ] Outbox has 6 items (or already sent)
- [ ] GitHub Actions workflow status
- [ ] 6 threads created in Firestore
- [ ] Outlook sent items show 6 outreach emails
- [ ] Sheet rows highlighted yellow

---

## Batch 1: Send ALL Broker Replies (YOU DO)

Send these 6 emails. **DO NOT wait between them** - send all 6, then wait for the next scheduled workflow run (or trigger manually).

### Email 1: 100 Commerce Way - COMPLETE (with attachments)
```
To: jill@mohrpartners.com
Subject: RE: 100 Commerce Way, Augusta

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
**Attach:** `test_pdfs/pdfs/full_e2e/100 Commerce Way - Property Flyer.pdf` AND `100 Commerce Way - Floor Plan.pdf`

---

### Email 2: 200 Industrial Blvd - PARTIAL (no attachments yet)
```
To: jill@mohrpartners.com
Subject: RE: 200 Industrial Blvd, Evans

Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```

---

### Email 3: 300 Warehouse Dr - UNAVAILABLE + NEW PROPERTY
```
To: jill@mohrpartners.com
Subject: RE: 300 Warehouse Dr, Augusta

Hi Jill,

Unfortunately, 300 Warehouse Dr is no longer available - just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. Let me know if you want details.

Mike Chen
mike.chen@augustacommercial.com
```

---

### Email 4: 400 Distribution Ave - IDENTITY QUESTION
```
To: jill@mohrpartners.com
Subject: RE: 400 Distribution Ave, Augusta

Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```

---

### Email 5: 500 Logistics Ln - TOUR OFFER
```
To: jill@mohrpartners.com
Subject: RE: 500 Logistics Ln, Evans

Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```

---

### Email 6: 600 Storage Ct - WRONG CONTACT
```
To: jill@mohrpartners.com
Subject: RE: 600 Storage Ct, Augusta

Hi Jill,

I no longer handle this property - I left Columbia Commercial last month. You'll want to reach out to Jennifer Adams who took over. Her email is jennifer.adams@columbiacommercial.com.

Good luck with your search!

Bob Thompson
```

---

### After Sending All 6:
- Trigger GitHub Actions workflow manually OR wait for scheduled run
- Wait until workflow completes

**Hand off to Claude: "Sent 6 broker replies, workflow completed"**

---

## Checkpoint 2: Claude Verifies Batch 1 Processing

I will check:
- [ ] **100 Commerce Way**: All fields extracted, row_completed notification, closing email sent, highlight cleared
- [ ] **200 Industrial Blvd**: Partial fields extracted (SF, height, docks, drive-ins), auto-reply sent requesting remaining
- [ ] **300 Warehouse Dr**: Moved to NON-VIABLE, new_property notification for 350 Tech Park
- [ ] **400 Distribution Ave**: needs_user_input:confidential notification, NO auto-reply
- [ ] **500 Logistics Ln**: tour_requested notification, NO auto-reply
- [ ] **600 Storage Ct**: wrong_contact notification, NO auto-reply
- [ ] Dashboard shows: 1 Complete, 3 Actions Needed

**Expected Sheet State After Batch 1:**

| Property | Total SF | Rent | Ops Ex | Docks | Drive-Ins | Height | Power | Status |
|----------|----------|------|--------|-------|-----------|--------|-------|--------|
| 100 Commerce Way | 25000 | 5.50 | 1.75 | 4 | 2 | 24 | 800 amps, 3-phase | COMPLETE |
| 200 Industrial Blvd | 18000 | - | - | 3 | 1 | 22 | - | PARTIAL |
| 400 Distribution Ave | - | - | - | - | - | - | - | PAUSED |
| 500 Logistics Ln | - | - | - | - | - | - | - | PAUSED |
| 600 Storage Ct | - | - | - | - | - | - | - | PAUSED |
| --- NON-VIABLE --- | | | | | | | | |
| 300 Warehouse Dr | - | - | - | - | - | - | - | UNAVAILABLE |

---

## Batch 2: Handle ALL Escalations (YOU DO)

Handle all 4 action items in the dashboard. **Do all of them before triggering the next workflow.**

### 2A: 400 Distribution Ave - Reply to Identity Question
Click notification, compose and send:
```
Hi James,

Thanks for reaching out. I represent a client in the metal distribution industry looking for warehouse space in the Augusta area. They prefer to keep company details confidential during the initial search, but I can tell you they're an established business with good credit.

Could you share the property details?

Thanks,
Jill
```

---

### 2B: 500 Logistics Ln - Accept Tour
Click notification, use/modify the suggested response:
```
Hi Karen,

Thank you for offering to show me the property! I'd love to schedule a tour.

Would any of these times work?
- Thursday at 10am
- Thursday at 2pm
- Friday at 10am

Please let me know what works best.

Thanks,
Jill
```

---

### 2C: 600 Storage Ct - Redirect to Jennifer
Click notification, verify the pre-filled email shows:
- **To:** jennifer.adams@columbiacommercial.com
- **Greeting:** "Hi Jennifer," (NOT "Hi [NAME],")
- **Referrer:** "Bob Thompson" or "Bob" (NOT "bob" or email prefix)

Approve and send.

---

### 2D: 350 Tech Park Dr - Approve New Property
Click the new property notification, verify:
- Address: 350 Tech Park Dr
- City: Augusta
- Contact: Mike Chen
- Email: mike.chen@augustacommercial.com

Approve to create row and send outreach.

---

### After All 4 Actions:
- Check Firestore outbox has 4 new items
- Trigger workflow OR wait for scheduled run
- Wait until workflow completes

**Hand off to Claude: "Handled all 4 escalations, workflow completed"**

---

## Checkpoint 3: Claude Verifies Batch 2 Processing

I will check:
- [ ] 4 emails sent from outbox (400, 500, 600, 350)
- [ ] 350 Tech Park Dr row created in sheet
- [ ] 600 Storage Ct email has correct name (not [NAME])
- [ ] Notifications updated/cleared appropriately

---

## Batch 3: Final Broker Replies (YOU DO)

Send these 2 emails to complete the multi-turn conversations:

### Email 7: 200 Industrial Blvd - COMPLETE REMAINING (with attachments)
```
To: jill@mohrpartners.com
Subject: RE: 200 Industrial Blvd, Evans

Hi Jill,

Here's the rest:
- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached.

Sarah
```
**Attach:** `test_pdfs/pdfs/full_e2e/200 Industrial Blvd - Property Flyer.pdf` AND `200 Industrial Blvd - Floor Plan.pdf`

---

### Email 8: 400 Distribution Ave - COMPLETE AFTER IDENTITY REPLY (with attachments)
```
To: jill@mohrpartners.com
Subject: RE: 400 Distribution Ave, Augusta

Hi Jill,

Thanks for the info on your client. Here are the full specs:

- Total Size: 30,000 SF
- Rate: $5.95/SF NNN
- Operating Expenses: $1.90/SF
- Loading: 6 docks, 2 drive-ins
- Clear Height: 26 ft
- Power: 1000 amps, 3-phase

Available in 60 days. See attached.

James
```
**Attach:** `test_pdfs/pdfs/full_e2e/400 Distribution Ave - Property Flyer.pdf` AND `400 Distribution Ave - Floor Plan.pdf`

---

### After Sending Both:
- Trigger workflow OR wait for scheduled run
- Wait until workflow completes

**Hand off to Claude: "Sent final 2 broker replies, workflow completed"**

---

## Checkpoint 4: Claude Verifies Final State

I will verify the complete campaign state:

### Sheet Final State

| Property | Total SF | Rent | Ops Ex | Docks | Drive-Ins | Height | Power | Flyer | Floorplan | Status |
|----------|----------|------|--------|-------|-----------|--------|-------|-------|-----------|--------|
| 100 Commerce Way | 25000 | 5.50 | 1.75 | 4 | 2 | 24 | 800 amps | Yes | Yes | COMPLETE |
| 200 Industrial Blvd | 18000 | 6.25 | 2.00 | 3 | 1 | 22 | 600 amps | Yes | Yes | COMPLETE |
| 400 Distribution Ave | 30000 | 5.95 | 1.90 | 6 | 2 | 26 | 1000 amps | Yes | Yes | COMPLETE |
| 500 Logistics Ln | - | - | - | - | - | - | - | - | - | TOUR SENT |
| 600 Storage Ct | - | - | - | - | - | - | - | - | - | REDIRECT SENT |
| 350 Tech Park Dr | - | - | - | - | - | - | - | - | - | OUTREACH SENT |
| --- NON-VIABLE --- | | | | | | | | | | |
| 300 Warehouse Dr | - | - | - | - | - | - | - | - | - | UNAVAILABLE |

### Dashboard Stats
- Properties Completed: 3 (100, 200, 400)
- Actions Needed: 0
- Non-Viable: 1 (300)

### Firestore Notifications
- 3x `row_completed` notifications
- 1x `property_unavailable` notification
- Handled events tracked on threads (no duplicates)

### Row Highlighting
- 100, 200, 400: No highlight (complete)
- 300: No highlight (non-viable)
- 500, 600, 350: May have highlight (awaiting broker reply)

---

## PDF Extraction Verification

Data from PDFs should match sheet values:

| Property | PDF Total SF | PDF Rent | PDF Ops | PDF Docks | PDF DriveIn | PDF Height | PDF Power |
|----------|--------------|----------|---------|-----------|-------------|------------|-----------|
| 100 Commerce Way | 25,000 | $5.50 | $1.75 | 4 | 2 | 24 ft | 800 amps, 3-phase |
| 200 Industrial Blvd | 18,000 | $6.25 | $2.00 | 3 | 1 | 22 ft | 600 amps, 3-phase |
| 400 Distribution Ave | 30,000 | $5.95 | $1.90 | 6 | 2 | 26 ft | 1000 amps, 3-phase |
| 350 Tech Park Dr | 22,000 | $5.75 | $1.85 | 3 | 2 | 20 ft | 500 amps, 3-phase |

---

## Test Complete Checklist

- [ ] All 6 initial outreach emails sent correctly
- [ ] PDF data extracted accurately (SF, rent, ops, docks, drive-ins, height, power)
- [ ] Flyer/Floorplan links populated from attachments
- [ ] Multi-turn conversation accumulated data correctly (200 Industrial)
- [ ] Property unavailable moved to NON-VIABLE (300 Warehouse)
- [ ] New property created from suggestion (350 Tech Park)
- [ ] Identity question escalated and user responded (400 Distribution)
- [ ] Tour request escalated and user accepted (500 Logistics)
- [ ] Wrong contact redirected with correct names (600 Storage)
- [ ] row_completed notifications created for finished properties
- [ ] No duplicate notifications
- [ ] Dashboard stats accurate
- [ ] Row highlighting behaves correctly

---

## Timing Summary

| Phase | Who | What |
|-------|-----|------|
| Setup | You | Upload Excel, start campaign |
| Checkpoint 1 | Claude | Verify initial state |
| Batch 1 | You | Send 6 broker replies |
| Checkpoint 2 | Claude | Verify processing, check sheet |
| Batch 2 | You | Handle 4 escalations |
| Checkpoint 3 | Claude | Verify emails sent correctly |
| Batch 3 | You | Send 2 final broker replies |
| Checkpoint 4 | Claude | Full verification, test complete |

**Total handoffs: 4** (not dozens of back-and-forth)
