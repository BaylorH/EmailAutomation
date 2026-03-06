# E2E Test Reply Cheatsheet

Send these replies to test each scenario. Use the email addresses shown.

---

## 1. 699 Industrial Park Dr (Complete Info + Custom Field)
**Send from:** bp21harrison@gmail.com
**Reply to:** The outreach email for this property

```
Hi Jill,

Thanks for reaching out about 699 Industrial Park Dr. Here are all the details:

- Total Size: 45,000 SF
- Asking Rate: $6.75/SF NNN
- Operating Expenses: $1.85/SF
- Loading: 4 dock-high doors, 2 drive-in doors
- Clear Height: 28'
- Power: 1200 amps, 480V 3-phase
- Parking: 85 spaces in the front lot

The property is available immediately.

Best regards,
Jeff Wilson
```

**Expected:** Row completes, closing email sent, **Parking Spaces = 85** (custom field test)

---

## 2. 135 Trade Center Court (REAL - Call Offer + PDFs)
**Send from:** bp21harrison@gmail.com
**Reply to:** The outreach email for this property
**Attach:** PDFs from `test_pdfs/real_world/` folder

```
Good Morning Jill,

This would certainly be a great fit here. Please see attached Building C & D plans.
We are asking $15/SF/NNN and we anticipate a delivery of July 1, 2025.

More than happy to jump on a call to discuss at your convenience. Just let me know what works best for you.

Luke Coffey
Sales Associate
Southeastern
p: (706)-854-6731
```

**Attach these files:**
- `Sealed Bldg C 10-24-23.pdf` (should go to Floorplan column)
- `Sealed Bldg D 10-24-23.pdf` (should go to Floorplan column)
- `135 Trade Center Court - Brochure.pdf` (should go to Flyer column)

**Expected:** Call offer detected, escalates to user (action_needed), PDFs categorized correctly

---

## 3. 2017 St. Josephs Drive (REAL - Unavailable + New Property)
**Send from:** baylor@manifoldengineering.ai
**Reply to:** The outreach email for this property

```
Hi Jill. I'm sorry but we are already at lease for this space, and it's our last one.
We also have an anchor restriction on fitness concepts.

However, below is the only current space we have, about 10 miles from Woodmore:

https://www.hp-llc.com/the-centre-at-forestville

Brian Greene
EVP OF LEASING
HP LLC
703-725-1351
```

**Expected:**
- Property marked unavailable, moved below NON-VIABLE
- New property suggestion detected with URL
- Action needed notification for new property approval

---

## 4. 9300 Lottsford Rd (REAL - Confidentiality Question)
**Send from:** baylor@manifoldengineering.ai
**Reply to:** The outreach email for this property
**Attach:** `Tapestry Largo Station Retail Floor Plan.pdf` from `test_pdfs/real_world/`

```
Hi Jill,

There is plenty of free retail parking on the first level of the garage.
Attached is the floor plan of the 1,400 SF space.

Please let us know what else you need.

By the way, what franchise is it that you are working with?

Thanks,
Craig S. Cheney
KLNB Commercial Real Estate Services
```

**Expected:**
- SF extracted (1400)
- Floor plan PDF goes to Floorplan column
- **CRITICAL:** "what franchise" = confidentiality question
- Must trigger `needs_user_input:confidential`
- NO auto-reply - pauses for user

---

## 5. 1 Randolph Ct (Wrong Contact)
**Send from:** bp21harrison@gmail.com

```
Hi Jill,

I no longer handle the listing at 1 Randolph Ct - I left Atkins Commercial last month.

You'll want to reach out to Mike Stevens who took over my listings.
His email is mike.stevens@atkinscommercial.com.

Good luck!
Scott
```

**Expected:** Wrong contact detected, action needed with new contact info

---

## 6. 1800 Broad St (Property Issue + Custom Field)
**Send from:** bp21harrison@gmail.com

```
Hi Jill,

Thanks for your interest in 1800 Broad St. I want to be upfront - we had some water damage in the rear section from a roof leak last month. About 2,000 SF is affected.

We're getting repairs done, expect completion in 3-4 weeks. The rest (18,000 SF) is fine.

Specs:
- Total: 20,000 SF
- $5.25/SF NNN, $1.50 CAM
- 2 docks, 2 drive-ins
- 20' clear, 600 amps
- Parking: 45 trailer spaces plus 30 car spots

Let me know if your client wants to wait or see the unaffected portion.

Marcus
```

**Expected:** Property issue detected (water damage), fields extracted, action needed, **Parking Spaces extracted** (custom field)

---

## 7. 2525 Center West Pkwy (Close Conversation)
**Send from:** baylor@manifoldengineering.ai

```
Hi Jill,

Thanks for reaching out about 2525 Center West Pkwy. Unfortunately, we've gone exclusive with another tenant rep on this property as of last week.

I wish you and your client the best of luck!

Lisa Anderson
```

**Expected:** Close conversation detected, no reply sent, thread closed

---

## Test Email Addresses

**Outbox/Sending Account:** `baylor.freelance@outlook.com` (acting as Jill)

**Broker reply accounts:**
| Property | Send Reply From |
|----------|-----------------|
| 699 Industrial Park Dr | bp21harrison@gmail.com |
| 135 Trade Center Court | bp21harrison@gmail.com |
| 2017 St. Josephs Drive | baylor@manifoldengineering.ai |
| 9300 Lottsford Rd | baylor@manifoldengineering.ai |
| 1 Randolph Ct | bp21harrison@gmail.com |
| 1800 Broad St | bp21harrison@gmail.com |
| 2525 Center West Pkwy | baylor@manifoldengineering.ai |

---

## Real PDFs Location

```
/Users/baylorharrison/Documents/GitHub/EmailAutomation/test_pdfs/real_world/
├── 135 Trade Center Court - Brochure.pdf    → Flyer column
├── Sealed Bldg C 10-24-23.pdf               → Floorplan column
├── Sealed Bldg D 10-24-23.pdf               → Floorplan column
└── Tapestry Largo Station Retail Floor Plan.pdf → Floorplan column
```
