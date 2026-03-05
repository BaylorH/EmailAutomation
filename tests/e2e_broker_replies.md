# E2E Test - Broker Reply Scripts

Copy-paste these replies from the appropriate email accounts.

---

## ROUND 1: Initial Broker Replies (send all 7)

### Reply 1: 699 Industrial Park Dr → COMPLETE INFO
**From:** bp21harrison@gmail.com
**Reply to:** Email with subject containing "699 Industrial Park Dr"

```
Hi Jill,

Thanks for reaching out about 699 Industrial Park Dr. Here are all the details:

- Total Size: 45,000 SF
- Asking Rate: $6.75/SF NNN
- Operating Expenses: $1.85/SF
- Loading: 4 dock-high doors, 2 drive-in doors
- Clear Height: 28'
- Power: 1200 amps, 480V 3-phase

The property is available immediately.

Best regards,
Jeff Wilson
```

---

### Reply 2: 135 Trade Center Court → PARTIAL INFO
**From:** bp21harrison@gmail.com
**Reply to:** Email with subject containing "135 Trade Center Court"

```
Hi Jill,

Yes, 135 Trade Center Court is available. It's 32,500 SF total with 6 dock doors and 2 drive-ins.

I need to check on the other specs - will get back to you.

Luke
```

---

### Reply 3: 2058 Gordon Hwy → UNAVAILABLE + NEW PROPERTY
**From:** baylor@manifoldengineering.ai
**Reply to:** Email with subject containing "2058 Gordon Hwy"

```
Hi Jill,

Unfortunately 2058 Gordon Hwy just went under contract last week - sorry about that!

However, I have another property that might work for your client - 3100 Peach Orchard Rd. It's a similar size warehouse, about 38,000 SF with good dock access. My colleague Sarah Chen handles that listing - you can reach her at sarah@meybohm.com.

Let me know if you'd like an introduction.

Jonathan
```

---

### Reply 4: 1 Kuhlke Dr → ESCALATION (asks questions)
**From:** baylor@manifoldengineering.ai
**Reply to:** Email with subject containing "1 Kuhlke Dr"

```
Hi Jill,

I'd be happy to provide details on 1 Kuhlke Dr. Before I do, can you tell me a bit about your client's business? What type of operation are they running and what's their timeline for moving in?

Also, what's their budget range for the space?

Thanks,
Robert McCrary
```

---

### Reply 5: 1 Randolph Ct → WRONG CONTACT
**From:** bp21harrison@gmail.com
**Reply to:** Email with subject containing "1 Randolph Ct"

```
Hi Jill,

I no longer handle the listing at 1 Randolph Ct - I left Atkins Commercial last month.

You'll want to reach out to Mike Stevens who took over my listings. His email is mike.stevens@atkinscommercial.com.

Good luck!
Scott
```

---

### Reply 6: 1800 Broad St → PROPERTY ISSUE
**From:** bp21harrison@gmail.com
**Reply to:** Email with subject containing "1800 Broad St"

```
Hi Jill,

Thanks for your interest in 1800 Broad St. I want to be upfront with you - we had some water damage in the rear section of the building from a roof leak last month. About 2,000 SF is affected.

We're currently getting repairs done and expect it to be completed in 3-4 weeks. The rest of the building (18K SF) is in good condition.

Here are the specs:
- Total: 20,000 SF
- $5.25/SF NNN
- $1.50 CAM
- 2 docks, 2 drive-ins
- 20' clear
- 600 amps

Let me know if your client wants to wait for repairs.

Marcus
```

---

### Reply 7: 2525 Center West Pkwy → CLOSE CONVERSATION (exclusive)
**From:** baylor@manifoldengineering.ai
**Reply to:** Email with subject containing "2525 Center West Pkwy"

```
Hi Jill,

Thanks for reaching out about 2525 Center West Pkwy. Unfortunately, we've gone exclusive with another tenant rep on this property as of last week. They're working with a client who's close to signing.

I wish you and your client the best of luck in your search!

Lisa Anderson
```

---

## ROUND 1 EXPECTED RESULTS

After triggering workflow:

| Property | Event | Sheet Update | Notification |
|----------|-------|--------------|--------------|
| 699 Industrial Park Dr | - | All fields | `row_completed` |
| 135 Trade Center Court | - | SF, Docks, Drive Ins | `sheet_update` |
| 2058 Gordon Hwy | `property_unavailable`, `new_property` | Moved to non-viable | `property_unavailable`, `action_needed` |
| 1 Kuhlke Dr | `needs_user_input` | None | `action_needed` |
| 1 Randolph Ct | `wrong_contact` | None | `action_needed` |
| 1800 Broad St | `property_issue` | All fields | `action_needed` |
| 2525 Center West Pkwy | `close_conversation` | None | `conversation_closed` |

---

## ROUND 2: Multi-Turn Completions

### Reply 2B: 135 Trade Center Court → COMPLETE REMAINING
**From:** bp21harrison@gmail.com
**Reply to:** The AI's follow-up email asking for remaining specs

```
Hey Jill,

Here's the rest of the info:
- NNN/CAM is $2.10/SF
- Clear height is 24 feet
- 800 amp 3-phase service

Let me know if you need anything else!

Luke
```

---

### Reply 4B: 1 Kuhlke Dr → USER SENDS REPLY FIRST
**Action:** In the Frontend UI:
1. Click the `action_needed` notification for 1 Kuhlke Dr
2. Use the chat to compose a reply OR manually edit
3. Send this reply:

```
Hi Robert,

My client is in the distribution business and looking to move within the next 3-4 months. They're flexible on budget - mainly focused on finding the right space with good loading access.

Could you share the property specs when you get a chance?

Thanks,
Jill
```

4. Click "Send Email" in the modal
5. Trigger workflow to send the email

---

### Reply 4C: 1 Kuhlke Dr → BROKER COMPLETES
**From:** baylor@manifoldengineering.ai
**Reply to:** The user's reply from step 4B

```
Perfect, that helps! Here's the info on 1 Kuhlke Dr:

- 28,000 SF
- $5.50/SF NNN
- $1.75 CAM
- 3 dock doors, 1 drive-in
- 22' clear
- 400 amps

Great loading access - perfect for distribution. Available in 60 days.

Robert
```

---

## ROUND 2 EXPECTED RESULTS

| Property | Event | Sheet Update | Notification |
|----------|-------|--------------|--------------|
| 135 Trade Center Court | - | Ops Ex, Ceiling, Power | `row_completed` |
| 1 Kuhlke Dr | - | All fields | `row_completed` |

---

## FINAL STATE

### Completed Rows (3)
- 699 Industrial Park Dr ✅
- 135 Trade Center Court ✅
- 1 Kuhlke Dr ✅

### Non-Viable (1)
- 2058 Gordon Hwy (moved below divider)

### Closed (1)
- 2525 Center West Pkwy (exclusive - no further action)

### Pending User Action (2)
- 1 Randolph Ct (wrong contact - needs manual resolution)
- 1800 Broad St (property issue - client decision needed)

### New Property Suggested (1)
- 3100 Peach Orchard Rd (from 2058 Gordon Hwy broker)
