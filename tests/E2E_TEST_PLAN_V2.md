# E2E Test Plan v2

## Improvements from v1
- **Batch actions**: You do multiple things at once, not one email at a time
- **Bug verification**: Explicitly check for double greeting and duplicate follow-up bugs
- **Campaign completion**: Verify dashboard shows campaign as complete
- **Notification cleanup**: Ensure all notifications are resolved at the end
- **Sheet verification**: Confirm all required fields are filled
- **Winner selection**: Simulate choosing a property as if this were real

---

## Test Data Setup

**Test File:** `test_pdfs/E2E_Test_V2.xlsx` (6 properties, same brokers to test multi-property)

| Property | Broker | Email | Scenario |
|----------|--------|-------|----------|
| 100 Commerce Way, Augusta | Tom Wilson | bp21harrison@gmail.com | Complete info |
| 200 Industrial Blvd, Evans | Sarah Miller | bp21harrison@gmail.com | Partial → Complete (multi-turn) |
| 300 Warehouse Dr, Augusta | Mike Chen | bp21harrison@gmail.com | Unavailable + new property |
| 400 Distribution Ave, Augusta | James Roberts | baylor@manifoldengineering.ai | Identity question (escalate) |
| 500 Logistics Ln, Evans | Karen Davis | baylor@manifoldengineering.ai | Tour request (escalate) |
| 600 Storage Ct, Augusta | Bob Thompson | baylor@manifoldengineering.ai | No response (test follow-ups) |

**Follow-up Config:** 3 follow-ups at 2 min, 3 min, 2 min

---

# PHASE 1: CAMPAIGN SETUP
**Owner: YOU**

1. Clear any existing test data (delete old test client if exists)
2. Upload `E2E_Test_V2.xlsx`
3. Configure follow-ups: 3 follow-ups, 2/3/2 minutes
4. Start campaign
5. **Tell me: "Campaign started"**

**I will verify:**
- [ ] 6 outbox items created
- [ ] Client created with correct followUpConfig
- [ ] No double greeting in any email scripts (BUG CHECK)

---

# PHASE 2: INITIAL SEND + VERIFICATION
**Owner: ME**

1. Trigger workflow
2. Wait for completion
3. **Verify:**
   - [ ] 6 threads created in Firestore
   - [ ] Outbox empty
   - [ ] All emails sent in Outlook
   - [ ] NO double greeting in multi-property emails (300 Warehouse, 600 Storage)
   - [ ] Correct contact names used in greetings

4. **Report to you with email samples**
5. **Tell you: "Ready for broker replies - send ALL 5 at once"**

---

# PHASE 3: BROKER REPLIES (ALL AT ONCE)
**Owner: YOU**

### Send ALL 5 replies in one batch, then tell me

**From bp21harrison@gmail.com:**

| Reply To | Content | Attachments |
|----------|---------|-------------|
| 100 Commerce Way | Complete info (25K SF, $5.50 NNN, $1.75 OpEx, 4 docks, 2 drive-ins, 24ft, 800A 3-phase) | Flyer + Floor plan |
| 200 Industrial Blvd | Partial info (18K SF, 22ft, 3 docks, 1 drive-in, "will get back on rate/power") | None |
| 300 Warehouse Dr | "Under contract, but I have 350 Tech Park Dr - 22K SF, similar specs" | None |

**From baylor@manifoldengineering.ai:**

| Reply To | Content | Attachments |
|----------|---------|-------------|
| 400 Distribution Ave | "Who is your client? We like to know who we're working with." | None |
| 500 Logistics Ln | "This sounds great! Available for a tour Thursday or Friday?" | None |

**DO NOT reply to 600 Storage Ct** (testing follow-ups)

**After sending all 5, tell me: "All 5 replies sent"**

---

# PHASE 4: PROCESS REPLIES + HANDLE ESCALATIONS
**Owner: ME then YOU**

### My Actions:
1. Trigger workflow
2. Wait for completion
3. **Verify Firestore:**
   - [ ] 100 Commerce: status=completed, all fields extracted
   - [ ] 200 Industrial: status=active, partial fields, follow-up scheduled
   - [ ] 300 Warehouse: status=completed (unavailable), new property notification
   - [ ] 400 Distribution: status=paused, action_needed notification
   - [ ] 500 Logistics: status=paused, action_needed notification
   - [ ] 600 Storage: status=active, follow-up waiting

4. **Check dashboard notifications:**
   - [ ] action_needed for 400 Distribution (identity question)
   - [ ] action_needed for 500 Logistics (tour request)
   - [ ] new_property for 350 Tech Park Dr
   - [ ] sheet_update notifications for extracted data
   - [ ] row_completed for 100 Commerce

5. **Report findings and tell you what needs action**

### Your Actions (ALL AT ONCE):
1. **400 Distribution** - Compose reply declining to reveal client (use AI chat)
2. **500 Logistics** - Accept tour for Thursday
3. **350 Tech Park** - Approve new property
4. **Dismiss** all sheet_update notifications

**Tell me: "All escalations handled"**

---

# PHASE 5: FOLLOW-UP VERIFICATION (ALL 3 FOLLOW-UPS)
**Owner: ME**

### Follow-up Schedule (configured: 2 min, 3 min, 2 min)
| Follow-up | Time After Initial | Time After Previous |
|-----------|-------------------|---------------------|
| FU1 | 2 min | - |
| FU2 | 5 min | 3 min |
| FU3 | 7 min | 2 min |

### Testing Sequence:

**Round 1 (~2 min after initial send):**
1. Trigger workflow
2. **Verify:**
   - [ ] 600 Storage: Follow-up #1 sent
   - [ ] Says "Hi Bob," NOT "Hi [NAME],"
   - [ ] NO duplicate (BUG CHECK)
   - [ ] 200 Industrial: Follow-up sent to Sarah (if no broker reply yet)

**Round 2 (~5 min after initial send):**
1. Trigger workflow
2. **Verify:**
   - [ ] 600 Storage: Follow-up #2 sent
   - [ ] Different content than FU1 ("Just a quick check-in...")
   - [ ] NO duplicate of FU1 (BUG CHECK)
   - [ ] currentFollowUpIndex = 2

**Round 3 (~7 min after initial send):**
1. Trigger workflow
2. **Verify:**
   - [ ] 600 Storage: Follow-up #3 sent (final)
   - [ ] Content mentions "final follow-up"
   - [ ] NO duplicate (BUG CHECK)
   - [ ] followUpStatus = "max_reached" after this
   - [ ] NO further follow-ups should be sent

**Final Check:**
1. Trigger workflow one more time
2. **Verify:**
   - [ ] NO follow-up #4 sent (should stop at 3)
   - [ ] Thread status reflects follow-up sequence complete

### Outlook Verification:
- [ ] Exactly 3 follow-up emails in 600 Storage thread
- [ ] Each has different content (FU1, FU2, FU3 templates)
- [ ] Proper spacing between them
- [ ] All say "Hi Bob," correctly

6. **Report findings with all 3 follow-up emails shown**

---

# PHASE 6: COMPLETE REMAINING CONVERSATIONS
**Owner: YOU**

### Send ALL remaining replies at once:

**From bp21harrison@gmail.com:**

| Reply To | Content | Attachments |
|----------|---------|-------------|
| 200 Industrial Blvd | Complete remaining: $6.25 NNN, $2.00 OpEx, 600A 3-phase | Flyer + Floor plan |
| 350 Tech Park Dr | Complete info: 22K SF, $7.00 NNN, $2.25 OpEx, 2 docks, 1 drive-in, 26ft, 400A 3-phase | Flyer |

**From baylor@manifoldengineering.ai:**

| Reply To | Content | Attachments |
|----------|---------|-------------|
| 500 Logistics (if system replied) | "Great, see you Thursday at 10am" | None |

**Tell me: "All completion replies sent"**

---

# PHASE 7: STOP 600 STORAGE + FINAL PROCESSING
**Owner: YOU then ME**

**NOTE:** Only do this AFTER Phase 5 confirms all 3 follow-ups sent and followUpStatus = "max_reached"

### Your Actions:
1. Open Conversations modal
2. Find 600 Storage Ct (should show 4 messages: initial + 3 follow-ups)
3. Click "Stop" button (to mark as manually stopped vs max_reached)
4. **Tell me: "600 Storage stopped"**

### My Actions:
1. Trigger workflow
2. Wait for completion
3. **Verify all threads final state:**
   - [ ] 100 Commerce: completed
   - [ ] 200 Industrial: completed
   - [ ] 300 Warehouse: completed (unavailable)
   - [ ] 350 Tech Park: completed (or active if awaiting more)
   - [ ] 400 Distribution: completed (declined identity)
   - [ ] 500 Logistics: completed (tour scheduled)
   - [ ] 600 Storage: stopped

---

# PHASE 8: CAMPAIGN COMPLETION VERIFICATION
**Owner: ME then YOU**

### Dashboard Verification:
1. **Stats Cards:**
   - [ ] "Campaigns Completed" shows 1 (or correct count)
   - [ ] "Properties Completed" shows correct count
   - [ ] "Active Conversations" shows 0

2. **Notifications:**
   - [ ] All action_needed cleared
   - [ ] Only row_completed notifications remaining (or dismissed)
   - [ ] No pending escalations

3. **Client Status:**
   - [ ] Client shows as "completed" or all threads resolved

### Your Action:
- Review dashboard, confirm it looks correct
- **Tell me: "Dashboard verified"**

---

# PHASE 9: SHEET ANALYSIS
**Owner: ME then YOU**

### I will fetch and analyze the Google Sheet:

**For each property, verify:**

| Property | Required Fields | Status |
|----------|-----------------|--------|
| 100 Commerce Way | SF, Rent, OpEx, Docks, Drive-ins, Height, Power, Flyer, Floorplan | Should be COMPLETE |
| 200 Industrial Blvd | SF, Rent, OpEx, Docks, Drive-ins, Height, Power, Flyer, Floorplan | Should be COMPLETE |
| 300 Warehouse Dr | Should be below NON-VIABLE divider | UNAVAILABLE |
| 350 Tech Park Dr | SF, Rent, OpEx, Docks, Drive-ins, Height, Power, Flyer | Should be COMPLETE |
| 400 Distribution Ave | May have partial/no data (declined) | DECLINED |
| 500 Logistics Ln | May have partial data + tour scheduled note | TOUR SCHEDULED |
| 600 Storage Ct | No data (stopped before response) | STOPPED |

### Your Action:
- Open the Google Sheet
- Confirm data matches what brokers provided
- **Tell me: "Sheet verified"**

---

# PHASE 10: CONVERSATION ANALYSIS
**Owner: ME**

### Full Outlook Analysis:
For each property, I will:
1. Fetch complete email thread from Outlook
2. Analyze each message for:
   - Correct greeting (no double "Hi")
   - Professional tone
   - Correct data acknowledgment
   - Appropriate escalation handling
3. Grade each conversation A-F
4. Note any bugs or issues

### Deliverable:
- Detailed analysis report for each conversation
- Overall quality score
- Bug checklist with pass/fail

---

# PHASE 11: WINNER SELECTION (SIMULATION)
**Owner: YOU + ME**

### Review viable properties:

| Property | SF | Rent | Total Cost | Pros | Cons |
|----------|----|----|------------|------|------|
| 100 Commerce Way | 25,000 | $5.50 | TBD | 800A power, 24ft | Larger than needed |
| 200 Industrial Blvd | 18,000 | $6.25 | TBD | Good size | Lower power (600A) |
| 350 Tech Park Dr | 22,000 | $7.00 | TBD | 26ft height | Higher rent |
| 500 Logistics Ln | TBD | TBD | TBD | Tour scheduled | Need to see it |

### Decision:
- You pick a "winner" based on client requirements (4-8K SF, 3-phase power, drive-in, 14ft+ height)
- We discuss if any properties actually fit
- Simulate "next steps" (schedule tour, request LOI, etc.)

---

# CHECKLIST SUMMARY

## Bug Checks:
- [ ] No double greeting in multi-property emails
- [ ] No duplicate follow-ups sent
- [ ] Contact names correctly extracted and used
- [ ] [NAME] placeholder always replaced

## Follow-up Sequence Checks:
- [ ] Follow-up #1 sends after 2 min wait
- [ ] Follow-up #2 sends after 3 min wait (different content)
- [ ] Follow-up #3 sends after 2 min wait (mentions "final")
- [ ] NO Follow-up #4 (stops at max)
- [ ] followUpStatus changes to "max_reached" after FU3
- [ ] Broker response pauses follow-up sequence correctly

## Flow Checks:
- [ ] All emails sent correctly
- [ ] Multi-turn conversations work
- [ ] Escalations trigger correctly
- [ ] Follow-ups send on schedule
- [ ] Stop button works
- [ ] New property flow works

## Completion Checks:
- [ ] Dashboard shows campaign complete
- [ ] All notifications resolved
- [ ] Sheet fully populated
- [ ] Conversations display correctly in modal

---

# TIMING

| Phase | Estimated |
|-------|-----------|
| 1-2: Setup + Initial Send | 5 min |
| 3-4: Broker Replies + Escalations | 10 min |
| 5: Follow-up Verification (ALL 3) | 10 min (2+3+2 min waits + triggers) |
| 6-7: Complete + Stop | 5 min |
| 8-9: Verification | 5 min |
| 10-11: Analysis + Winner | 10 min |

**Total: ~45-50 minutes**

---

# READY?

When you're ready, do Phase 1 and tell me **"Campaign started"**.
