# Fresh E2E Test Plan

## Test Properties (6)

| Row | Property | City | Broker | Email | Scenario |
|-----|----------|------|--------|-------|----------|
| 3 | 100 Commerce Way | Augusta | Tom Wilson | bp21harrison@gmail.com | Complete on first reply |
| 4 | 200 Industrial Blvd | Evans | Sarah Miller | bp21harrison@gmail.com | Multi-turn (partial → complete) |
| 5 | 300 Warehouse Dr | Augusta | Mike Chen | bp21harrison@gmail.com | Unavailable + new property |
| 6 | 400 Distribution Ave | Augusta | James Roberts | baylor@manifoldengineering.ai | Identity question → complete |
| 7 | 500 Logistics Ln | Evans | Karen Davis | baylor@manifoldengineering.ai | Tour offered |
| 8 | 600 Storage Ct | Augusta | Bob Thompson | baylor@manifoldengineering.ai | Wrong contact (left company) |

---

## Conversation Flow

### ROUND 1: System sends initial outreach (6 emails)
User action: Launch campaign from dashboard

### ROUND 2: Broker replies (you send 6 emails)

**Email 1: 100 Commerce Way - COMPLETE INFO**
```
To: baylor@manifoldengineering.ai
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
Attachments: 100_commerce_way_flyer.pdf, 100_commerce_way_floorplan.pdf

---

**Email 2: 200 Industrial Blvd - PARTIAL INFO**
```
To: baylor@manifoldengineering.ai
Subject: RE: 200 Industrial Blvd, Evans

Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```
NO attachments yet

---

**Email 3: 300 Warehouse Dr - UNAVAILABLE + NEW PROPERTY**
```
To: baylor@manifoldengineering.ai
Subject: RE: 300 Warehouse Dr, Augusta

Hi Jill,

Unfortunately, 300 Warehouse Dr just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. My email is mike.chen@augustacommercial.com if you want details.

Mike Chen
```
NO attachments

---

**Email 4: 400 Distribution Ave - IDENTITY QUESTION**
```
To: baylor@manifoldengineering.ai
Subject: RE: 400 Distribution Ave, Augusta

Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```
NO attachments

---

**Email 5: 500 Logistics Ln - TOUR OFFER**
```
To: baylor@manifoldengineering.ai
Subject: RE: 500 Logistics Ln, Evans

Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```
NO attachments

---

**Email 6: 600 Storage Ct - WRONG CONTACT**
```
To: baylor@manifoldengineering.ai
Subject: RE: 600 Storage Ct, Augusta

Hi Jill,

I no longer handle this property - I left Columbia Commercial last month. You'll want to reach out to Jennifer Adams who took over. Her email is jennifer.adams@columbiacommercial.com.

Good luck with your search!

Bob Thompson
```
NO attachments

---

### ROUND 3: System processes and responds
Run: `python main.py`

**Expected Results:**
- 100 Commerce Way → row_completed, closing email sent
- 200 Industrial Blvd → requests remaining fields (rate, power, ops ex)
- 300 Warehouse Dr → property_unavailable + new_property notification
- 400 Distribution Ave → needs_user_input:confidential (pauses)
- 500 Logistics Ln → tour_requested notification (pauses)
- 600 Storage Ct → wrong_contact:left_company notification

---

### ROUND 4: Continue conversations (you send 3 more emails)

**Email 7: 200 Industrial Blvd - COMPLETE REMAINING**
```
To: baylor@manifoldengineering.ai
Subject: RE: 200 Industrial Blvd, Evans

Hi Jill,

Here's the rest:
- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached flyer and floor plan.

Sarah
```
Attachments: 200_industrial_blvd_flyer.pdf, 200_industrial_blvd_floorplan.pdf

---

**User Action: Reply to 400 Distribution Ave identity question**
(From dashboard, compose reply about client being in metal distribution)

**Email 8: 400 Distribution Ave - BROKER COMPLETES AFTER USER REPLY**
```
To: baylor@manifoldengineering.ai
Subject: RE: 400 Distribution Ave, Augusta

Hi Jill,

Thanks for the info on your client. Here are the full specs:

- Total Size: 30,000 SF
- Rate: $5.95/SF NNN
- Operating Expenses: $1.90/SF
- Loading: 6 docks, 2 drive-ins
- Clear Height: 26 ft
- Power: 1000 amps, 3-phase

Available in 60 days. Attached are the flyer and floor plan.

James
```
Attachments: 400_distribution_ave_flyer.pdf, 400_distribution_ave_floorplan.pdf

---

### ROUND 5: System processes final replies
Run: `python main.py`

**Expected Results:**
- 200 Industrial Blvd → row_completed
- 400 Distribution Ave → row_completed

---

## Final Campaign State

| Property | Status | Notifications |
|----------|--------|---------------|
| 100 Commerce Way | ✅ COMPLETE | row_completed |
| 200 Industrial Blvd | ✅ COMPLETE | row_completed |
| 300 Warehouse Dr | ⛔ NON-VIABLE | property_unavailable, new_property |
| 400 Distribution Ave | ✅ COMPLETE | needs_user_input → row_completed |
| 500 Logistics Ln | ⏸️ PAUSED | tour_requested |
| 600 Storage Ct | ⏸️ PAUSED | wrong_contact |

**Sheet should show:**
- 3 rows complete (100, 200, 400)
- 1 row moved to NON-VIABLE (300)
- 2 rows awaiting user action (500, 600)
- 1 new property pending approval (350 Tech Park)

---

## PDF Files Needed

Convert these HTML files to PDF (Cmd+P → Save as PDF):

```
test_pdfs/htmls/
├── 100_commerce_way_flyer.html      → 100_commerce_way_flyer.pdf
├── 100_commerce_way_floorplan.html  → 100_commerce_way_floorplan.pdf
├── 200_industrial_blvd_flyer.html   → 200_industrial_blvd_flyer.pdf
├── 200_industrial_blvd_floorplan.html → 200_industrial_blvd_floorplan.pdf
├── 400_distribution_ave_flyer.html  → 400_distribution_ave_flyer.pdf
├── 400_distribution_ave_floorplan.html → 400_distribution_ave_floorplan.pdf
```

(350 Tech Park PDFs only needed if you approve the new property)
