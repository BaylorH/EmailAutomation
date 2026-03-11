# E2E Test Reply Cheatsheet

Send these replies to test each scenario. Copy-paste ready.

**Test File:** `test_pdfs/E2E_Real_World_Test.xlsx`

---

## Custom Field Modes Being Tested

| Column | UI Setting | Internal Mode | Behavior |
|--------|------------|---------------|----------|
| **Rail Access** | Ask + Required ✓ | `ask_required` | Blocks closing if missing |
| **Office %** | Ask (no Required) | `ask_optional` | AI asks but doesn't block |
| **Building Condition Notes** | Note | `note` | Auto-append, never ask |

---

## 1. 699 Industrial Park Dr (ALL Custom Fields Complete)
**Send from:** bp21harrison@gmail.com

```
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
**Attach:** Any PDF

**Expected:**
- Standard fields extracted: SF=45000, Ops Ex=8.50, Ceiling=28, Docks=4, Drive Ins=2, Power=400A 3-phase
- **Rail Access** = "CSX spur" (ask_required ✓)
- **Office %** = 15 (ask_optional ✓)
- **Building Condition Notes** = "roof replaced, newer HVAC" (note - auto-appended)
- Leasing Contact stays as original (NOT Bob Martinez)
- Closing email sent, row_completed

---

## 2. 150 Trade Center Court (Unavailable + New Property)
**Send from:** bp21harrison@gmail.com

```
Hey - bad news, 150 Trade Center just went under contract last week.

I might have something else that could work though - there's a
new development on Trade Center Court, similar specs. Want me
to send you info on that?
```
**No attachment**

**Expected:**
- `property_unavailable` detected
- Row moved below NON-VIABLE divider
- AI responds expressing interest in alternative
- Notification: property_unavailable

---

## 3. 2017 St. Josephs Drive (Note Mode + Identity Question)
**Send from:** baylor@manifoldengineering.ai

```
2017 St. Josephs is 18,500 SF. The building was renovated in 2021 -
new roof, updated electrical, and the loading docks were completely
rebuilt. Previous tenant kept it in great shape.

Just a heads up - the HVAC system is original from 1998 and might
need attention in the next few years.

Who's this for by the way? Just want to make sure there aren't
any conflicts on our end.
```
**No attachment**

**Expected:**
- SF=18500 extracted
- **Building Condition Notes** = "Renovated 2021, new roof, updated electrical, rebuilt docks. HVAC original 1998" (AUTO-APPENDED)
- `needs_user_input:confidential` detected
- NO auto-reply (pauses for user)
- Notification: action_needed

**KEY TEST:** Note mode should auto-append WITHOUT asking

---

## 4. 9300 Lottsford Rd (URL as Flyer)
**Send from:** baylor@manifoldengineering.ai

```
Here's the rundown on 9300 Lottsford:
- 42,000 SF
- $6.75/SF NNN
- 30' clear
- 8 dock doors, 2 drive-ins
- 400A 3-phase

The building has direct rail access via CSX.
About 5% office build-out.

Full brochure is on our website:
https://example.com/listings/9300-lottsford

Let me know if you have questions.
```
**No attachment**

**Expected:**
- All standard fields extracted
- **Rail Access** = "direct CSX" (ask_required ✓)
- **Office %** = 5 (ask_optional ✓)
- **Flyer / Link** = URL extracted
- Closing email OR request for attachment

---

## 5. 1 Randolph Ct - Reply #1 (Partial)
**Send from:** bp21harrison@gmail.com

```
The space is 22,000 SF with 24' clear. NNN is $2.50/SF.
```
**No attachment**

**Expected:**
- SF=22000, Ceiling=24, Ops Ex=2.50 extracted
- AI requests remaining: Docks, Drive Ins, Power, Flyer, Rail Access
- Follow-up scheduled

---

## 5b. 1 Randolph Ct - Reply #2 (More Partial)
**Send from:** bp21harrison@gmail.com
**Send after:** Follow-up email arrives

```
Sorry for the delay - 3 docks, 1 drive-in.
Still checking on the power situation.
```
**No attachment**

**Expected:**
- Docks=3, Drive Ins=1 accumulated
- AI requests remaining: Power, Flyer, Rail Access
- Follow-up continues

---

## 5c. 1 Randolph Ct - Reply #3 (Standard Complete, Missing Rail)
**Send from:** bp21harrison@gmail.com
**Send after:** Follow-up email arrives

```
Power is 200A 3-phase. 1 drive-in door. Here's the flyer.
About 10% office space in the front.
```
**Attach:** Any PDF

**Expected:**
- Power=200A 3-phase, Flyer populated
- **Office %** = 10 (ask_optional ✓)
- **Rail Access** = ❌ MISSING
- AI **ASKS for Rail Access** (NOT close!)
- **KEY TEST:** ask_required blocks closing

---

## 5d. 1 Randolph Ct - Reply #4 (Rail Provided)
**Send from:** bp21harrison@gmail.com
**Send after:** AI asks about rail

```
No rail access at this location - closest rail is about 5 miles away.
```
**No attachment**

**Expected:**
- **Rail Access** = "No rail, closest 5 miles"
- Closing email sent (NOW all required fields complete)
- row_completed notification

---

## 6. 1800 Broad St (Identity Question)
**Send from:** bp21harrison@gmail.com

```
Thanks for reaching out about 1800 Broad.

Before I send over the details, it would help to know a bit more
about what you're looking for - is this for a specific tenant you're
working with, or more of a general search? Just want to make sure
I'm sending relevant info.

Mike
```
**No attachment**

**Expected:**
- `needs_user_input:confidential` detected
- NO auto-reply
- Notification: action_needed
- Follow-up paused until user responds

---

## 6b. 1800 Broad St - Final Reply (After User Handles)
**Send from:** bp21harrison@gmail.com
**Send after:** User sends reply through UI

```
No problem, I understand. Here's what we've got:

1800 Broad is 35,000 SF, asking $9/SF NNN. 26' clear height,
4 dock doors and 1 drive-in. Power is 400A 3-phase.

Building is on the NS rail line - active siding.
About 20% is finished office space.

The loading area was just repaved and there's a new
membrane roof as of last summer.

Flyer attached.
```
**Attach:** Any PDF

**Expected:**
- All standard fields extracted
- **Rail Access** = "NS rail line, active siding" (ask_required ✓)
- **Office %** = 20 (ask_optional ✓)
- **Building Condition Notes** = "Loading repaved, new membrane roof" (note ✓)
- Closing email, row_completed

---

## 7. 2525 Center West Pkwy (Tour + Optional Field Test)
**Send from:** baylor@manifoldengineering.ai

```
2525 Center West is a great space - 38,000 SF, $7.25 NNN,
32' clear with 6 docks and 2 drive-ins. Power is 480V 3-phase.
Building has rail access - Norfolk Southern siding on the east end.

I'm actually going to be at the property Thursday around 2pm
if your client wants to take a quick walk through. No pressure
either way - just let me know.

Flyer attached.
```
**Attach:** Any PDF

**Expected:**
- All standard fields extracted
- **Rail Access** = "Norfolk Southern siding" (ask_required ✓)
- **Office %** = ❌ MISSING (ask_optional - should NOT block)
- `tour_requested` detected
- NO auto-reply (user handles tour)
- Notification: action_needed (tour)

**KEY TEST:** Missing optional field doesn't block tour flow

---

## Test Email Addresses

| Account | Role |
|---------|------|
| `baylor.freelance@outlook.com` | Sends outreach (acting as Jill) |
| `bp21harrison@gmail.com` | Fake broker replies |
| `baylor@manifoldengineering.ai` | Fake broker replies |

| Property | Send Reply From |
|----------|-----------------|
| 699 Industrial Park Dr | bp21harrison@gmail.com |
| 150 Trade Center Court | bp21harrison@gmail.com |
| 2017 St. Josephs Drive | baylor@manifoldengineering.ai |
| 9300 Lottsford Rd | baylor@manifoldengineering.ai |
| 1 Randolph Ct | bp21harrison@gmail.com |
| 1800 Broad St | bp21harrison@gmail.com |
| 2525 Center West Pkwy | baylor@manifoldengineering.ai |

---

## Success Criteria Summary

| Mode | Column | Must Pass |
|------|--------|-----------|
| **ask_required** | Rail Access | Blocks closing at 1 Randolph Reply #3, allows after #4 |
| **ask_optional** | Office % | Extracted when provided, missing doesn't block at 2525 |
| **note** | Building Condition Notes | Auto-appends at 699, 2017, 1800 - NEVER asked |
