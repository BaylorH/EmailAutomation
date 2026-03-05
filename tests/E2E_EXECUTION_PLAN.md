# E2E Test Execution Plan

## Roles
- **YOU (Baylor)**: UI actions, sending broker reply emails
- **ME (Claude)**: Workflow triggers, Firestore audits, Outlook audits, quality analysis

---

# PHASE 1: CAMPAIGN SETUP
**Owner: YOU**

### Your Actions (do all at once):
1. Open the admin UI at localhost:3000
2. Click "Add Client"
3. Upload `test_pdfs/E2E_Test_Augusta.xlsx`
4. Configure follow-ups: **3 follow-ups** with times **2 min, 3 min, 2 min**
5. Start the campaign
6. **Tell me: "Campaign started"**

### What I'll Do:
- Verify outbox has 6 items
- Verify client created with correct followUpConfig
- Proceed to Phase 2

---

# PHASE 2: INITIAL EMAIL SEND
**Owner: ME**

### My Actions:
1. Trigger workflow
2. Wait for completion
3. Audit Firestore:
   - 6 threads created
   - All have contactName, rowNumber, status: active
   - Outbox empty
4. Audit Outlook SentItems:
   - 6 emails sent
   - Correct subjects and greetings
5. **Report findings and paste initial email samples**

### Your Action:
- Review my audit, confirm emails look good
- **Tell me: "Proceed to replies"**

---

# PHASE 3: BROKER REPLIES (BATCH)
**Owner: YOU**

### IMPORTANT: Send ALL 5 replies at once, then tell me.

**From bp21harrison@gmail.com, send these 3 replies:**

---

**Reply 1 → Reply to "100 Commerce Way, Augusta"**
Attach: `test_pdfs/pdfs/full_e2e/100 Commerce Way - Property Flyer.pdf` and `100 Commerce Way - Floor Plan.pdf`
```
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

---

**Reply 2 → Reply to "200 Industrial Blvd, Evans"**
No attachments
```
Hi Jill,

Thanks for reaching out. Here's what I have on 200 Industrial Blvd:

- Total Size: 18,000 SF
- Clear Height: 22 ft
- 3 dock doors, 1 drive-in

I need to check on the rate and power specs - will get back to you.

Sarah
```

---

**Reply 3 → Reply to "300 Warehouse Dr, Augusta"**
No attachments
```
Hi Jill,

Unfortunately, 300 Warehouse Dr just went under contract last week.

However, I have another property that might work - 350 Tech Park Dr. It's 22,000 SF with similar specs. Let me know if you want details.

Mike Chen
```

---

**From baylor@manifoldengineering.ai, send these 2 replies:**

---

**Reply 4 → Reply to "400 Distribution Ave, Augusta"**
No attachments
```
Hi Jill,

Thanks for your interest in 400 Distribution Ave. Before I send details, can you tell me who your client is? We like to know who we're working with.

James Roberts
```

---

**Reply 5 → Reply to "500 Logistics Ln, Evans"**
No attachments
```
Hi Jill,

500 Logistics Ln sounds like a great fit for your client. I'd love to show them the space - are you available for a tour this Thursday or Friday?

Karen Davis
```

---

**DO NOT reply to 600 Storage Ct** (that's for the Stop test)

### After sending all 5:
**Tell me: "All 5 replies sent"**

---

# PHASE 4: PROCESS REPLIES & FULL AUDIT
**Owner: ME**

### My Actions:
1. Trigger workflow
2. Wait for completion
3. **Full Firestore Audit:**
   - Thread statuses (completed, active, paused)
   - Message counts per thread
   - Notification types and reasons
   - Data cleanliness check
4. **Full Outlook Audit:**
   - Fetch all conversations
   - Paste full conversation for each property
   - Score conversation quality
5. **Sheet Data Check:**
   - What got extracted to sheet
   - Verify values match expected
6. **Report comprehensive findings**

### Your Action:
- Review my audit report
- **Tell me: "Proceed to Stop test"**

---

# PHASE 5: STOP CONVERSATION TEST
**Owner: YOU**

### Your Actions:
1. Open the Conversations modal for the client
2. Find "600 Storage Ct" thread (should show Active badge)
3. Click the "Stop" button
4. Confirm the dialog
5. **Tell me: "600 Storage stopped"**

### What I'll Do:
- Verify thread status changed to "stopped"
- Verify followUpStatus is "paused"
- Confirm no follow-up will be sent

---

# PHASE 6: FOLLOW-UP TEST
**Owner: ME (with wait)**

### My Actions:
1. Note the time
2. Wait 2+ minutes (for 200 Industrial follow-up to be due)
3. Trigger workflow
4. **Audit follow-up:**
   - Was follow-up sent for 200 Industrial?
   - Does it say "Hi Sarah" (not [NAME])?
   - Was follow-up NOT sent for stopped/paused/completed threads?
5. **Paste and analyze follow-up email content**
6. **Report findings**

### Your Action:
- Review follow-up quality
- **Tell me: "Proceed to complete 200 Industrial"**

---

# PHASE 7: COMPLETE 200 INDUSTRIAL
**Owner: YOU**

### Your Action:
**From bp21harrison@gmail.com, reply to the 200 Industrial thread:**

Attach: `test_pdfs/pdfs/full_e2e/200 Industrial Blvd - Property Flyer.pdf` and `200 Industrial Blvd - Floor Plan.pdf`
```
Hi Jill,

Here's the rest of the info on 200 Industrial Blvd:

- Rate: $6.25/SF NNN
- Operating Expenses: $2.00/SF
- Power: 600 amps, 3-phase

See attached flyer and floor plan.

Sarah
```

**Tell me: "200 Industrial reply sent"**

---

# PHASE 8: FINAL PROCESSING & COMPLETE AUDIT
**Owner: ME**

### My Actions:
1. Trigger workflow
2. Wait for completion
3. **Complete Final Audit:**

   **Firestore:**
   - All thread final statuses
   - Total message counts
   - All notifications created
   - Check for any data bloat/orphans

   **Outlook:**
   - Fetch complete conversations for all 6 properties
   - Paste full email threads
   - Analyze conversation quality
   - Check closing emails sent

   **Sheet:**
   - Screenshot/export final sheet state
   - Verify all extracted values
   - Check NON-VIABLE placement

   **Notifications:**
   - List all notifications
   - Cross-reference with conversation events
   - Check for missing/extra notifications

4. **Generate Scoring Report:**
   - Fill out scoring template
   - Calculate final scores
   - List issues found
   - Production readiness assessment

---

# SUMMARY: YOUR ACTIONS CHECKLIST

| Phase | Your Action | Tell Claude |
|-------|-------------|-------------|
| 1 | Upload Excel, configure follow-ups, start campaign | "Campaign started" |
| 2 | (wait for my audit) | "Proceed to replies" |
| 3 | Send ALL 5 broker replies (batch) | "All 5 replies sent" |
| 4 | (wait for my audit) | "Proceed to Stop test" |
| 5 | Click Stop on 600 Storage Ct | "600 Storage stopped" |
| 6 | (wait for my audit) | "Proceed to complete 200 Industrial" |
| 7 | Send 200 Industrial completion reply with PDFs | "200 Industrial reply sent" |
| 8 | (wait for final audit) | Review final report |

---

# TIMING ESTIMATE

| Phase | Duration |
|-------|----------|
| Phase 1: Setup | 2-3 min |
| Phase 2: Initial Send + Audit | 3-5 min |
| Phase 3: Send Replies | 5-10 min |
| Phase 4: Process + Full Audit | 5-10 min |
| Phase 5: Stop Test | 1 min |
| Phase 6: Follow-up Wait + Audit | 3-5 min |
| Phase 7: Complete 200 Industrial | 2-3 min |
| Phase 8: Final Audit + Report | 10-15 min |

**Total: ~35-50 minutes**

---

# TOOLS I'LL USE

```bash
# Trigger workflow
python3 tests/e2e_helpers.py trigger

# Check status
python3 tests/e2e_helpers.py status

# Check workflow completion
python3 tests/e2e_helpers.py workflow

# Firestore inspection
curl -s "https://email-token-manager.onrender.com/api/firestore-inspect"

# Fetch Outlook conversations (I'll write custom queries)
# - SentItems for Jill's outbound
# - Check thread message history
```

---

# READY TO START?

When you're ready, do Phase 1 and tell me **"Campaign started"**.
