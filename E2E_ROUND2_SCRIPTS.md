# E2E Round 2 - Edge Case Test Scripts

## Overview

Round 2 tests the remaining edge cases not covered in Round 1:
- `call_requested` event
- `wrong_contact` event (forwarded to colleague)
- `property_issue` event (major issue)
- Automatic follow-up system (non-responsive broker)
- Natural conversation close (broker ends without completion)

**File:** `Scrub Augusta GA - Round 2.xlsx`

---

## Property → Scenario Mapping

| Row | Property | Contact | Email Account | Test Scenario |
|-----|----------|---------|---------------|---------------|
| 3 | 250 Peach Orchard Rd | David Chen | bp21harrison@gmail.com | **Call Requested** |
| 4 | 1500 Walton Way | Maria Rodriguez | bp21harrison@gmail.com | **Wrong Contact (Forwarded)** |
| 5 | 3200 Washington Rd | Tom Bradley | baylor@manifoldengineering.ai | **Property Issue (Major)** |
| 6 | 450 Broad St | Silent Sam Wilson | baylor@manifoldengineering.ai | **Non-Responsive (Follow-up Test)** |
| 7 | 800 Reynolds St | Jennifer Park | bp21harrison@gmail.com | **Natural Close** |

---

## Scenario H: Call Requested
**Property:** 250 Peach Orchard Rd
**Send from:** bp21harrison@gmail.com
**Reply to thread:** "250 Peach Orchard Rd, Augusta"

### Turn 1 - Broker asks for phone call:
```
Hi,

Thanks for reaching out about 250 Peach Orchard Rd. I've got some details to share but would prefer to discuss over the phone - there are some nuances about the space that are easier to explain verbally.

Can you give me a call at (706) 555-8234? I'm available most afternoons.

Best,
David Chen
Augusta Industrial
```

**Expected Result:**
- `call_requested` event detected
- Phone number captured: (706) 555-8234
- `action_needed` notification with reason=call_requested
- AI does NOT auto-reply (waits for user decision)

**User Action:** User sees notification, decides to call or send alternative response

---

## Scenario I: Wrong Contact (Forwarded)
**Property:** 1500 Walton Way
**Send from:** bp21harrison@gmail.com
**Reply to thread:** "1500 Walton Way, Augusta"

### Turn 1 - Wrong person, forwards to colleague:
```
Hi,

I no longer handle the Walton Way property - I moved to our residential division last month.

I've forwarded your inquiry to my colleague James Martinez who took over my commercial listings. His email is james.martinez@waltonproperties.com.

Maria Rodriguez
```

**Expected Result:**
- `wrong_contact` event detected with subreason `forwarded`
- `action_needed` notification created
- Notification includes new contact info (James Martinez, email)
- AI does NOT auto-send to new contact (requires approval)

**MODAL TEST:**
- Modal shows wrong contact info
- Shows suggested action: "Contact James Martinez"
- User can approve to send to new contact or dismiss

### Turn 2 (Optional) - After user approves new contact:
```
Hi,

Thanks for reaching out about 1500 Walton Way. Maria passed along your inquiry.

Here's what I have:
- Total SF: 14,500
- Ceiling: 22' clear
- Docks: 2
- Drive-ins: 1
- Power: 300 amps, 3-phase
- OpEx: $2.75/SF NNN

Let me know if you need anything else.

James Martinez
Walton Properties
```

**Expected Result:**
- Fields extracted normally
- Leasing Contact field: Still shows "Maria Rodriguez" (original, not overwritten)
- Conversation continues as normal

---

## Scenario J: Property Issue (Major)
**Property:** 3200 Washington Rd
**Send from:** baylor@manifoldengineering.ai
**Reply to thread:** "3200 Washington Rd, Augusta"

### Turn 1 - Broker mentions significant property issue:
```
Hi,

The space at 3200 Washington Rd is available, but I should mention upfront - we had some roof damage from the storm last month. The repairs are scheduled but won't be complete for another 6-8 weeks. There's been some water intrusion in the northeast corner.

If that timeline works for your client, here are the specs:
- Total SF: 18,000
- Ceiling: 20' clear
- Docks: 3
- Drive-ins: 2
- Power: 400 amps, 3-phase
- OpEx: $3.00/SF NNN

Let me know if you want to proceed or if you'd prefer to wait until repairs are done.

Tom Bradley
Washington Commercial
```

**Expected Result:**
- `property_issue` event detected with severity=major
- Issue description captured: "roof damage, water intrusion, 6-8 week repair timeline"
- `action_needed` notification with reason=property_issue:major
- Fields STILL extracted (Total SF, etc.)
- AI may or may not auto-reply depending on implementation
- Notes added to Listing Brokers Comments about the issue

**Verification:**
- User can see property issue details in notification
- User decides whether to continue pursuing or mark non-viable

---

## Scenario K: Non-Responsive (Follow-up Test)
**Property:** 450 Broad St
**Send from:** (No response - broker doesn't reply)
**Purpose:** Test automatic follow-up system

### Setup:
After initial outreach is sent, DO NOT send any broker reply.

**Expected Timeline (if follow-up system is configured):**
- Day 0: Initial outreach sent
- Day 3: First follow-up sent automatically
- Day 7: Second follow-up sent automatically
- Day 14: Final follow-up sent automatically

**Note:** This scenario requires waiting for follow-up timers OR manually advancing time. May need to be tested separately or verified via logs showing follow-up scheduling.

**Verification:**
- Check that thread has `nextFollowupAt` timestamp set
- Check follow-up email content is appropriate (gentle nudge, not aggressive)
- Verify follow-up count doesn't exceed max

---

## Scenario L: Natural Close
**Property:** 800 Reynolds St
**Send from:** bp21harrison@gmail.com
**Reply to thread:** "800 Reynolds St, Augusta"

### Turn 1 - Broker provides partial info:
```
Hi,

Reynolds Distribution is 25,000 SF with 24' clear ceilings. Great space for distribution.

Let me pull together the other specs and get back to you.

Jennifer Park
Reynolds Partners
```

**Expected Result:**
- Fields extracted: Total SF=25000, Ceiling Ht=24
- AI sends follow-up for remaining fields

### Turn 2 - Broker naturally ends conversation:
```
Actually, I just found out we're going exclusive with another tenant rep who already has a client lined up. They're likely signing the lease next week.

Sorry I couldn't help this time - feel free to reach out if you have other properties you're looking for in the area.

Best,
Jennifer
```

**Expected Result:**
- `close_conversation` event detected
- Reason: "exclusive_with_another" or similar
- `conversation_closed` notification created
- AI does NOT send follow-up (conversation terminated)
- Thread marked as closed/inactive
- Property remains in sheet (not moved to NON-VIABLE since technically still available, just exclusive)

**Verification:**
- Conversation properly ended
- No further automated emails sent
- Thread status reflects closed state

---

## Email Account Reference

| Account | Used For |
|---------|----------|
| baylor.freelance@outlook.com | **System account** - sends all outbound |
| bp21harrison@gmail.com | Simulate: David Chen, Maria Rodriguez, James Martinez, Jennifer Park |
| baylor@manifoldengineering.ai | Simulate: Tom Bradley, Silent Sam Wilson |

---

## Round 2 Success Criteria

| Scenario | Event Type | Must Verify |
|----------|------------|-------------|
| H: Call Requested | `call_requested` | Phone number captured, no auto-reply |
| I: Wrong Contact | `wrong_contact:forwarded` | New contact info captured, approval flow |
| J: Property Issue | `property_issue:major` | Issue details captured, fields still extracted |
| K: Non-Responsive | N/A | Follow-up scheduled/sent |
| L: Natural Close | `close_conversation` | Thread properly terminated, no more emails |

---

## Round 2 Execution Notes

1. **Round 1 must complete first** - Run all Round 1 scenarios before starting Round 2
2. **Different client** - Create a new client with the Round 2 file
3. **Same verification approach** - Trigger workflow after each reply, verify Firestore/Sheets/Outlook
4. **Follow-up test is time-dependent** - May need to check logs for scheduled follow-ups rather than waiting

---

## Combined Test Report Structure

After both rounds complete, final report includes:

```markdown
# Full E2E Test Report

## Round 1 Results
- Scenarios: A-G (7 scenarios)
- Properties: 6 (5 original + 1 new)
- Events tested: 6 types

## Round 2 Results
- Scenarios: H-L (5 scenarios)
- Properties: 5
- Events tested: 4 additional types

## Total Coverage
- Scenarios: 12
- Properties: 11
- Event types: 10/10 covered
- Modals: 8+ tested
- Edge cases: Comprehensive
```
