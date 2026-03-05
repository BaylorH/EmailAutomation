# Comprehensive E2E Test Plan

This test validates the entire email automation campaign from start to finish, testing all major scenarios the system handles.

---

## Test Setup

### Prerequisites
1. Fresh Google Sheet with test properties
2. Clean Firestore state (no leftover notifications)
3. Clean Outlook inbox (archive old test emails)

### Test Properties (6 rows)

| Row | Property | City | Broker | Email | Scenario |
|-----|----------|------|--------|-------|----------|
| 3 | 100 Commerce Way | Augusta | Tom Wilson | bp21harrison@gmail.com | Complete on first reply |
| 4 | 200 Industrial Blvd | Evans | Sarah Miller | bp21harrison@gmail.com | Multi-turn (partial -> complete) |
| 5 | 300 Warehouse Dr | Augusta | Mike Chen | bp21harrison@gmail.com | Unavailable + new property |
| 6 | 400 Distribution Ave | Augusta | James Roberts | baylor@manifoldengineering.ai | Identity question -> complete |
| 7 | 500 Logistics Ln | Evans | Karen Davis | baylor@manifoldengineering.ai | Tour offered |
| 8 | 600 Storage Ct | Augusta | Bob Thompson | baylor@manifoldengineering.ai | Wrong contact (left company) |

---

## Round 1: Launch Campaign

**User Action:** Click "Start Campaign" in dashboard

**Expected Result:**
- 6 initial outreach emails sent
- All 6 rows highlighted yellow (system managing)
- Threads created in Firestore

---

## Round 2: Send Broker Replies

Send these 6 emails from the test accounts. Wait for GitHub Actions workflow to complete between rounds.

### Email 1: 100 Commerce Way - COMPLETE INFO
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

Available immediately.

Tom Wilson
```
**Attachments:** 100_commerce_way_flyer.pdf, 100_commerce_way_floorplan.pdf

---

### Email 2: 200 Industrial Blvd - PARTIAL INFO
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

## Round 2 Expected Results

After `python main.py` processes these:

| Property | Action | Dashboard |
|----------|--------|-----------|
| 100 Commerce Way | All fields extracted, closing email sent | row_completed notification, highlight cleared |
| 200 Industrial Blvd | Partial fields extracted, auto-reply requests rest | sheet_update notifications |
| 300 Warehouse Dr | Moved to NON-VIABLE, asks about alternatives | property_unavailable + new_property notifications |
| 400 Distribution Ave | Paused - escalated to user | needs_user_input:confidential notification |
| 500 Logistics Ln | Paused - escalated to user | tour_requested notification |
| 600 Storage Ct | Paused - escalated to user | wrong_contact notification |

**Dashboard Stats Should Show:**
- Properties Completed: 1 (100 Commerce Way)
- Actions Needed: 3 (400, 500, 600)

---

## Round 3: User Actions

### 3A: Handle 400 Distribution Ave (Identity Question)

**User Action:** Click notification, compose reply:
```
Hi James,

Thanks for reaching out. I represent a client in the metal distribution industry looking for warehouse space in the Augusta area. They prefer to keep company details confidential during the initial search, but I can tell you they're an established business with good credit.

Could you share the property details?

Thanks,
Jill
```

**Expected:** Email queued to outbox

---

### 3B: Handle 500 Logistics Ln (Tour Request)

**User Action:** Click notification, accept tour suggestion:
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

**Expected:** Email queued to outbox

---

### 3C: Handle 600 Storage Ct (Wrong Contact)

**User Action:** Click notification, approve redirect email to Jennifer Adams

**Verify:**
- Email shows "Hi Jennifer," (not "Hi [NAME],")
- Referrer shows "Bob Thompson" or "Bob" (not "bob" or email prefix)

**Expected:** Email queued to outbox

---

### 3D: Handle 350 Tech Park Dr (New Property)

**User Action:** Click notification for new property suggestion, approve and send outreach

**Expected:** New row created, outreach email sent

---

## Round 4: Continue Conversations

Run workflow, then send these broker replies:

### Email 7: 200 Industrial Blvd - COMPLETE REMAINING
```
To: jill@mohrpartners.com
Subject: RE: 200 Industrial Blvd, Evans

Hi Jill,

Here's the rest:
- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

Sarah
```
**Attachments:** 200_industrial_blvd_flyer.pdf, 200_industrial_blvd_floorplan.pdf

---

### Email 8: 400 Distribution Ave - COMPLETE AFTER USER REPLY
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

Available in 60 days.

James
```
**Attachments:** 400_distribution_ave_flyer.pdf, 400_distribution_ave_floorplan.pdf

---

## Round 4 Expected Results

| Property | Action | Dashboard |
|----------|--------|-----------|
| 200 Industrial Blvd | All fields complete, closing email sent | row_completed notification |
| 400 Distribution Ave | All fields complete, closing email sent | row_completed notification |

**Dashboard Stats Should Show:**
- Properties Completed: 3 (100, 200, 400)

---

## Final State Verification

### Google Sheet
| Property | Status | Data Complete? |
|----------|--------|----------------|
| 100 Commerce Way | VIABLE | Yes - all fields filled |
| 200 Industrial Blvd | VIABLE | Yes - all fields filled |
| 400 Distribution Ave | VIABLE | Yes - all fields filled |
| 500 Logistics Ln | VIABLE | No - tour in progress |
| 600 Storage Ct | VIABLE | No - redirect in progress |
| 350 Tech Park Dr | VIABLE | No - awaiting reply |
| --- NON-VIABLE --- | divider | |
| 300 Warehouse Dr | NON-VIABLE | N/A - unavailable |

### Dashboard
- Properties Completed: 3
- Actions Needed: 0 (all handled)
- Non-Viable: 1

### Row Highlighting
- 100, 200, 400: No highlight (complete)
- 300: No highlight (non-viable)
- 500, 600: No highlight (user took action, waiting reply)
- 350: Yellow highlight (system waiting for reply)

---

## Bugs to Watch For

1. **Duplicate notifications** - Same event shouldn't create multiple notifications
2. **[NAME] placeholder** - Should be replaced in all sent emails
3. **Referrer name** - Should show actual name, not email prefix
4. **Self-referral** - When broker suggests own property, don't say "X mentioned you"
5. **Row highlighting** - Yellow while system manages, clear when complete/escalated
6. **row_completed notifications** - Should be created when closing email sent

---

## Test Completion Checklist

- [ ] All 6 initial outreach emails sent
- [ ] Row highlighting applied on send
- [ ] Complete info extracted correctly (100 Commerce Way)
- [ ] Multi-turn conversation works (200 Industrial Blvd)
- [ ] NON-VIABLE row movement works (300 Warehouse Dr)
- [ ] New property suggestion creates notification (350 Tech Park)
- [ ] Identity question escalates correctly (400 Distribution Ave)
- [ ] Tour request escalates correctly (500 Logistics Ln)
- [ ] Wrong contact escalates correctly (600 Storage Ct)
- [ ] User can respond to escalations
- [ ] Redirect email shows correct name (not [NAME])
- [ ] row_completed notifications created
- [ ] Dashboard stats accurate
- [ ] No duplicate notifications
- [ ] Row highlighting cleared appropriately
