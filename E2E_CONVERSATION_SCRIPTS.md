# E2E Test Conversation Scripts

## Property → Scenario Mapping

| Row | Property | Contact | Email Account | Test Scenario |
|-----|----------|---------|---------------|---------------|
| 3 | 699 Industrial Park Dr, Evans | Jeff Wilson | bp21harrison@gmail.com | **Complete Info (1 turn)** + Voluntary Rent |
| 4 | 135 Trade Center Court, Augusta | Luke Coffey | bp21harrison@gmail.com | **Partial → Complete (2 turns)** |
| 5 | 2058 Gordon Hwy, Augusta | Jonathan Aceves | baylor@manifoldengineering.ai | **Unavailable + New Property** |
| 6 | 1 Kuhlke Dr, Augusta | Robert McCrary | baylor@manifoldengineering.ai | **Long Conversation (5+ turns)** + Tour Offer |
| 7 | 1 Randolph Ct, Evans | Scott Atkins | bp21harrison@gmail.com | **Escalation (Identity Question)** + ChatWithAI |
| NEW | 500 Bobby Jones Expressway | Mike Johnson | bp21harrison@gmail.com | **New Property Reply** + Tour Requested |

### Additional Edge Case Tests (Post-Scenarios)
| Test | Property | What We Test |
|------|----------|--------------|
| Tour Requested | 500 Bobby Jones | `tour_requested` event, suggested email modal |
| Contact Optout | Any closed thread | Broker says "remove me" after conversation |
| Forbidden Fields | All | Verify rent NEVER requested, Gross Rent NEVER written |

---

## Scenario A: Complete Info (1 Turn) + Voluntary Rent Verification
**Property:** 699 Industrial Park Dr, Evans
**Send from:** bp21harrison@gmail.com
**Send to:** baylor.freelance@outlook.com
**Reply to thread:** "699 Industrial Park Dr, Evans"

### Turn 1 - Broker Reply (provides all info INCLUDING rent voluntarily):
```
Hi Jill,

Happy to help! Here's the info on 699 Industrial Park Dr:

- Total SF: 15,000
- Ceiling Height: 24' clear
- Docks: 2 dock-high doors
- Drive-ins: 1 grade-level door
- Power: 400 amps, 3-phase
- Ops Ex: $2.50/SF NNN
- Asking Rent: $7.50/SF/yr

The space is available immediately. I can send over the flyer if you'd like.

Best,
Jeff Wilson
```

**Expected Result:**
- Sheet row 3 filled with all values
- **Rent/SF /Yr: 7.50** (captured because voluntarily provided)
- **Gross Rent: AUTO-CALCULATED** (formula field, never written directly)
- Closing/thank you email sent
- `row_completed` notification created

**CRITICAL VERIFICATION:**
- ✅ AI captured rent because broker volunteered it
- ✅ AI did NOT request rent (check response email has no rent question)
- ✅ Gross Rent column is formula-calculated, not AI-written

---

## Scenario B: Partial → Complete (2 Turns)
**Property:** 135 Trade Center Court, Augusta
**Send from:** bp21harrison@gmail.com
**Send to:** baylor.freelance@outlook.com
**Reply to thread:** "135 Trade Center Court, Augusta"

### Turn 1 - Broker Reply (partial info):
```
Hi,

The space at 135 Trade Center Court is 12,000 SF with 20' clear ceiling height.

I'll have to check on the other details and get back to you.

Thanks,
Luke
```

**Expected Result:**
- Sheet: Total SF = 12000, Ceiling Ht = 20
- AI sends follow-up requesting missing fields
- `sheet_update` notifications created

### Turn 2 - Broker Reply (completes info):
```
Got those details for you:
- 2 dock doors
- 1 drive-in
- Power: 200 amps, single phase
- NNN: $1.85/SF

That should be everything!

Luke
```

**Expected Result:**
- Sheet row 4 completed
- Closing email sent
- `row_completed` notification

---

## Scenario C: Unavailable + New Property Suggestion
**Property:** 2058 Gordon Hwy, Augusta
**Send from:** baylor@manifoldengineering.ai
**Send to:** baylor.freelance@outlook.com
**Reply to thread:** "2058 Gordon Hwy, Augusta"

### Turn 1 - Broker Reply (unavailable + suggests new property):
```
Hi,

Unfortunately 2058 Gordon Hwy just went under contract last week.

However, I do have another listing at 500 Bobby Jones Expressway that might work - it's 22,000 SF with similar specs. The contact there is Mike Johnson at mike@augusta-realty.com.

Would you like info on that one?

Best,
Jonathan
```

**Expected Result:**
- 2058 Gordon Hwy moved below NON-VIABLE divider
- `property_unavailable` notification created
- `action_needed` notification for new property (pending approval)
- NO row created for 500 Bobby Jones yet (pending user approval)

### After User Approves New Property:
- Row created for 500 Bobby Jones Expressway
- Email sent to mike@augusta-realty.com

### Turn 2 - New Property Broker Reply (from Mike) - WITH TOUR OFFER:
**Send from:** bp21harrison@gmail.com (simulating Mike)
**Reply to thread:** "500 Bobby Jones Expressway, Augusta"

```
Hi,

Thanks for reaching out about 500 Bobby Jones Expressway. Here's what I have:

- Total SF: 22,000
- Ceiling: 20' clear
- Docks: 3 dock-high
- Drive-ins: 1
- Power: 600 amps, 3-phase
- OpEx: $2.25/SF

I'd love to show this space to your client. I have availability this Thursday or Friday afternoon. Would either of those work for a tour?

Mike Johnson
Augusta Commercial Realty
```

**Expected Result:**
- Sheet fields extracted: Total SF=22000, Ceiling Ht=20, Docks=3, Drive Ins=1, Power=600 amps 3-phase, Ops Ex=2.25
- **`tour_requested` event detected**
- **`action_needed` notification created with `reason=tour_requested`**
- **Suggested response email generated** (pre-filled in modal)
- AI does NOT auto-reply (waits for user to confirm tour)

**MODAL TEST: Tour Request Modal**
- Modal shows the tour offer context
- Shows suggested response email (e.g., "Thank you for the offer. Let me check with my client...")
- User can edit response before sending
- Approve button sends the response

---

## Scenario D: Long Conversation (5+ Turns)
**Property:** 1 Kuhlke Dr, Augusta
**Send from:** baylor@manifoldengineering.ai
**Send to:** baylor.freelance@outlook.com
**Reply to thread:** "1 Kuhlke Dr, Augusta"

### Turn 1 - Broker Reply (vague acknowledgment):
```
Yeah we have that space available. Nice building.

Let me know if you want to discuss.

Robert
```

**Expected Result:**
- No field updates (no concrete data)
- AI asks for specifics

### Turn 2 - Broker Reply (partial info):
```
Sure thing. Off the top of my head:
- It's about 8,000 SF total
- Ceiling is around 18 feet I think

I'll need to check with property management on the other specs.

Robert
```

**Expected Result:**
- Sheet: Total SF = 8000, Ceiling Ht = 18
- AI requests remaining fields

### Turn 3 - Broker Reply (more info):
```
Got some answers back:
- Power: 400 amps, 3-phase
- We do have a floorplan available

One thing to note - the current tenant is using about 2,000 SF for office buildout. Would that work for your client or do they need the full warehouse space?

Robert
```

**Expected Result:**
- Sheet: Power updated
- AI might escalate the question OR answer it contextually
- Notes added about office buildout

### Turn 4 - Broker Reply (after AI response):
```
Good to know that works. Here are the remaining details:

- Docks: 1 dock door
- Drive-ins: 2 drive-in doors
- OpEx: $3.15/SF NNN

The space can be available in 60 days with some notice.

Robert
```

**Expected Result:**
- Sheet: Docks, Drive Ins, Ops Ex updated
- May still be missing some fields

### Turn 5 - Broker Reply (final details):
```
Almost forgot - here's the flyer link: https://example.com/1kuhlke-flyer.pdf

Asking rent is $6.25/SF/yr.

Let me know if your client wants to tour.

Robert
```

**Expected Result:**
- All fields complete
- Closing email sent
- `row_completed` notification
- **Thread has 5+ back-and-forth exchanges**

---

## Scenario E: Escalation (Identity Question)
**Property:** 1 Randolph Ct, Evans
**Send from:** bp21harrison@gmail.com
**Send to:** baylor.freelance@outlook.com
**Reply to thread:** "1 Randolph Ct, Evans"

### Turn 1 - Broker Reply (asks identity question):
```
Hi,

Before I share more details, can you tell me who your client is? We typically need to know who we're working with before providing specific pricing.

Thanks,
Scott
```

**Expected Result:**
- `action_needed` notification with reason `needs_user_input:confidential`
- **NO automatic reply sent** (AI pauses for user)
- Thread paused

### User Action: Send Response via Dashboard
User composes reply in the modal, something like:
```
I represent a confidential industrial tenant looking for 15,000+ SF in the Augusta area.
```

### Turn 2 - Broker Reply (accepts, provides info):
```
Thanks for clarifying - industrial distribution makes sense for this space.

Here's what I have:
- Total SF: 18,500
- Ceiling Height: 24' clear
- Docks: 2 dock-high doors
- Drive-ins: 1 grade-level door
- NNN: $2.85/SF
- Power: 200 amps

Let me know if you need anything else.

Best,
Sarah
```

**Note:** Sarah replies (different name) - tests that Leasing Contact is NOT overwritten.

**Expected Result:**
- Sheet row 7 filled (but Leasing Contact still says "Scott A. Atkins")
- AI sends follow-up for any missing fields OR closes
- Conversation resumes normally

---

## Scenario F: Tour Requested (500 Bobby Jones - Follow-up)
**Property:** 500 Bobby Jones Expressway (created in Scenario C)
**Send from:** bp21harrison@gmail.com (simulating Mike)

This scenario continues from Scenario C Turn 2 above. After processing Mike's tour offer:

### User Action: Respond to Tour Request via Modal

1. Open Dashboard notification sidebar
2. Click the `tour_requested` notification for 500 Bobby Jones
3. Modal should show:
   - Tour offer context ("Thursday or Friday afternoon")
   - Pre-filled suggested response
4. Edit or approve the response
5. Click Send

**Expected Result:**
- Response sent as threaded reply
- `action_needed` notification resolved
- If all fields complete, row may be marked complete

---

## Scenario G: Contact Optout (Edge Case)
**Purpose:** Test what happens when a broker says "don't contact me"
**Property:** Any completed property (we'll use 699 Industrial Park Dr)
**Send from:** bp21harrison@gmail.com (as Jeff Wilson)

### Setup:
After completing all main scenarios, send a follow-up hostile reply:

```
Please remove me from your mailing list. We don't work with tenant reps.

Jeff Wilson
```

**Expected Result:**
- `contact_optout` event detected with subreason `no_tenant_reps`
- Contact added to opt-out list
- `action_needed` notification created
- Future campaigns should skip this contact

---

## Scenario H: Call Requested (Edge Case)
**Purpose:** Test call request escalation (different from identity question)
**Property:** Can be tested on any property mid-conversation

### Example Reply:
```
I'd rather discuss this over the phone. Can you give me a call at (706) 555-1234?

Thanks,
[Broker]
```

**Expected Result:**
- `call_requested` event detected
- `action_needed` notification with phone number captured
- AI does NOT auto-reply
- User must decide to call or decline

---

## Email Account Reference

| Account | Used For |
|---------|----------|
| baylor.freelance@outlook.com | **System account** - sends all outbound |
| bp21harrison@gmail.com | Simulate brokers: Jeff Wilson, Luke Coffey, Scott Atkins, Mike Johnson |
| baylor@manifoldengineering.ai | Simulate brokers: Jonathan Aceves, Robert McCrary |

---

## Execution Checklist

### Before Each Turn:
- [ ] Claude provides the broker reply script (above)
- [ ] User copies script
- [ ] User sends from correct email account
- [ ] User replies to the correct thread (check subject line)

### After Each Turn:
- [ ] User triggers GitHub workflow
- [ ] User pastes full logs
- [ ] Claude verifies: Firestore, Sheets, Outlook, Notifications
- [ ] Claude reports pass/fail for turn

### At End of Each Scenario:
- [ ] Claude confirms scenario criteria met
- [ ] Claude snapshots final state
- [ ] Proceed to next scenario
