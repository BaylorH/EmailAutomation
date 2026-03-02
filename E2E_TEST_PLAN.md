# E2E Campaign Test Plan

## Overview

This document defines the structure, success criteria, and execution plan for a comprehensive end-to-end test of the email automation campaign system.

**Test Scope:** Full campaign lifecycle from client creation through conversation completion
**Environment:** Production (baylor.freelance@outlook.com)
**Verification Tools:** Firestore, Google Sheets, Outlook API, Workflow Logs

---

## Part 1: Success Criteria Rubric

### 1.1 Campaign Launch

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| Client created in Firestore | Query `users/{uid}/clients/{clientId}` | Document exists with correct metadata |
| Google Sheet created | Check `excelUrl` field + Sheet API access | Sheet accessible, headers match config |
| Outbox entries created | Query `users/{uid}/outbox/` | One entry per property/broker |
| Emails sent | Check Outlook Sent Items | All outbox items sent, deleted from outbox |
| Threads indexed | Query `threads/`, `msgIndex/`, `convIndex/` | Each sent email has thread + indexes |
| Row numbers assigned | Check thread `rowNumber` fields | Each thread points to correct sheet row |

### 1.2 Conversation Handling

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| Inbound matched to thread | Workflow logs + `msgIndex` lookup | "Matched via ConversationId/MessageId" in logs |
| Messages stored | Query `threads/{id}/messages/` | All inbound/outbound messages saved |
| AI extraction accurate | Compare Sheet values to email content | Extracted values match what broker stated |
| Response appropriate | Check Outlook Sent for reply | Professional, requests missing fields or closes |
| No forbidden fields requested | Review AI response text | Never asks for Rent/SF/Yr or Gross Rent |
| Leasing Contact unchanged | Check Sheet after different person replies | Original contact preserved |

### 1.3 Multi-Turn Conversations (5+ exchanges)

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| Thread continuity | All messages in same thread doc | No orphaned messages or duplicate threads |
| Cumulative extraction | Sheet values accumulate over turns | Later replies add to earlier extractions |
| Conversation memory | AI references earlier context | Response acknowledges previous info |
| No duplicate sends | Check Outlook Sent Items | One response per inbound message |

### 1.4 Property Completion

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| All required fields filled | Check Sheet row | Total SF, Ops Ex, Drive Ins, Docks, Ceiling Ht, Power |
| Closing email sent | Check Outlook Sent | Thank you message, conversation ends |
| `row_completed` notification | Query notifications collection | Notification created with correct rowAnchor |
| No further processing | Subsequent workflow runs | Thread skipped, no new emails |

### 1.5 Non-Viable Properties

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| NON-VIABLE divider exists | Check Sheet | Row with "NON-VIABLE" in column A |
| Row moved below divider | Check Sheet row positions | Property row below divider row |
| Thread rowNumber updated | Check Firestore thread doc | rowNumber matches new sheet position |
| Other threads adjusted | Check all thread rowNumbers | Threads above moved row shift correctly |
| `property_unavailable` notification | Query notifications | Notification created |
| Comment added to row | Check Sheet column | "[DATE] Property marked unavailable" note |

### 1.6 New Property Suggestions

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| `action_needed` notification | Query notifications | kind=action_needed, reason=new_property_pending_approval |
| Notification has property data | Check notification meta | Address, city, contact, email populated |
| No row created yet | Check Sheet | Row only created after user approval |
| Approval creates row | After user action, check Sheet | New row inserted above NON-VIABLE |
| Email sent to new contact | Check Outlook Sent | Outreach to suggested contact |
| Thread created for new property | Query threads | New thread with correct rowNumber |

### 1.7 Escalation Events

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| `needs_user_input` detected | Notification created | Correct subreason (confidential, client_question, etc.) |
| No auto-reply sent | Check Outlook Sent | AI does NOT respond automatically |
| User response sent as reply | After user action, check Sent | Threaded reply, not new email |
| Conversation resumes | Next broker reply processed | Thread continues normally |

### 1.8 Threading Integrity

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| Replies use `createReply` | Workflow logs | "Sending as REPLY to thread" message |
| Same conversation ID | Check Outlook conversation threading | All messages in same thread |
| Message IDs indexed | Query `msgIndex/` | Each message ID maps to thread |
| No orphaned messages | Compare Outlook vs Firestore | All Outlook messages exist in Firestore |

### 1.9 Notification Accuracy

| Criterion | How to Verify | Pass Condition |
|-----------|---------------|----------------|
| `sheet_update` per field change | Query notifications | One notification per extracted field |
| Correct before/after values | Check notification meta | oldValue/newValue match reality |
| Priority set correctly | Check notification priority | Important for actions, normal for updates |
| Frontend receives in real-time | User confirms dashboard | Notifications appear without refresh |

---

## Part 2: Execution Structure

### Phase 0: Pre-Test Setup
**Owner:** Claude
**Duration:** ~5 minutes

1. Snapshot current Firestore state (threads, notifications)
2. Snapshot current Sheet state (all rows)
3. Verify all tools working (Firestore, Sheets, Outlook)
4. Document baseline

**Checkpoint:** Claude reports ready to begin

---

### Phase 1: Campaign Launch
**Owner:** User (with Claude verification)

#### User Actions:
1. Create new client via Dashboard (or use existing clean client)
2. Upload Excel file with test properties
3. Configure columns in mapping step
4. Launch campaign (Start Project)

#### Claude Verification (after user confirms launch):
- [ ] Query Firestore: client doc created
- [ ] Query Firestore: outbox entries exist
- [ ] Check Sheet: headers and rows match
- [ ] Wait for workflow or user triggers it

**Checkpoint:** User triggers workflow, pastes logs

#### Claude Verification (after workflow):
- [ ] Analyze logs: all emails sent successfully
- [ ] Check Outlook Sent: emails actually delivered
- [ ] Query Firestore: threads created with correct rowNumbers
- [ ] Query Firestore: outbox emptied
- [ ] Query indexes: msgIndex and convIndex populated

**Checkpoint:** Claude reports Phase 1 complete, ready for broker replies

---

### Phase 2: Broker Replies (Multi-Scenario)
**Owner:** User sends broker replies, Claude provides scripts

For each test scenario, Claude provides the broker reply text.
User sends from appropriate email account.
User triggers workflow and pastes logs.

#### Scenario A: Complete Info (Single Turn)
- Broker provides all required fields
- Expected: Sheet filled, closing email sent, row_completed notification

#### Scenario B: Partial Info (Multi-Turn)
- Broker provides some fields
- Expected: AI requests missing fields
- User sends follow-up broker reply with remaining info
- Expected: Sheet completed, closing email

#### Scenario C: Property Unavailable
- Broker says property is no longer available
- Expected: Row moved below NON-VIABLE, notification created

#### Scenario D: New Property Suggestion
- Broker suggests different property with new contact
- Expected: action_needed notification, no row created yet
- User approves via Dashboard
- Expected: Row created, email sent to new contact

#### Scenario E: Escalation (Identity Question)
- Broker asks "who is your client?"
- Expected: needs_user_input notification, NO auto-reply
- User sends response via Dashboard
- Expected: Response sent as threaded reply
- Broker replies with info
- Expected: Conversation resumes normally

#### Scenario F: Long Conversation (5+ turns)
- Multiple back-and-forth exchanges
- Expected: Thread integrity maintained, cumulative extraction

**Checkpoint Structure for Each Scenario:**
1. Claude provides broker reply text
2. User sends email
3. User triggers workflow
4. User pastes logs
5. Claude analyzes logs + checks Firestore/Sheets/Outlook
6. Claude reports pass/fail for scenario criteria
7. Proceed to next scenario or address issues

---

### Phase 3: Campaign Completion
**Owner:** Claude (verification)

After all scenarios complete:

1. Verify all properties resolved (complete OR non-viable)
2. Verify no pending notifications requiring action
3. Verify no orphaned threads or messages
4. Verify Sheet state matches expected final state
5. Generate summary report

---

## Part 3: Checkpoint Protocol

### When Claude Pauses for User:

1. **Before sending broker replies** - Claude provides text, waits for user to send
2. **After workflow runs** - User pastes logs, Claude analyzes
3. **Dashboard actions needed** - User must approve/decline/respond
4. **Any blocking issue** - Claude reports issue, proposes solution

### What Claude Checks Automatically:

At each checkpoint, Claude runs these verifications without user input:

```
1. Firestore Queries:
   - threads/ collection state
   - notifications/ collection state
   - outbox/ collection state (should be empty after send)
   - msgIndex/ and convIndex/ state

2. Google Sheets:
   - Row data for each property
   - Row positions relative to NON-VIABLE divider
   - Column values match expected extractions

3. Outlook:
   - Recent Inbox messages
   - Recent Sent messages
   - Verify threading (conversation grouping)

4. Cross-Reference:
   - Firestore messages match Outlook messages
   - Thread rowNumbers match Sheet positions
   - Notifications match actual events
```

---

## Part 4: Test Properties

For a comprehensive test, we need properties that will go through different scenarios:

| Property | Broker Email | Planned Scenario |
|----------|--------------|------------------|
| Property A | broker1@test.com | Complete info (1 turn) |
| Property B | broker2@test.com | Partial → Complete (2 turns) |
| Property C | broker3@test.com | Unavailable → Non-viable |
| Property D | broker4@test.com | Suggests new property |
| Property E | broker5@test.com | Escalation (identity question) |
| Property F | broker6@test.com | Long conversation (5+ turns) |

*Actual email addresses to be determined based on test setup*

---

## Part 5: Final Report Template

After test completion, Claude generates:

```markdown
# E2E Test Report - [DATE]

## Summary
- Total Properties Tested: X
- Scenarios Passed: X/X
- Issues Found: X

## Results by Scenario

### Scenario A: Complete Info
- Status: PASS/FAIL
- Turns: 1
- Fields Extracted: [list]
- Closing Email Sent: Yes/No
- Notification Created: Yes/No

[Repeat for each scenario]

## Issues Found
1. [Issue description + impact + recommended fix]

## Feature Verification Checklist
- [ ] Campaign launch
- [ ] Email sending
- [ ] Thread matching
- [ ] AI extraction
- [ ] Multi-turn conversations
- [ ] Property completion
- [ ] Non-viable handling
- [ ] New property suggestions
- [ ] Escalation flow
- [ ] Notification accuracy
- [ ] Row number consistency
- [ ] Threading integrity

## Production Readiness
[ ] Ready for production
[ ] Issues to address first (list below)
```

---

## Appendix: Quick Reference Commands

### Claude's Verification Commands

**Check all threads:**
```python
threads = db.collection('users').document(user_id).collection('threads').stream()
```

**Check Sheet rows:**
```python
result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range="'FOR LEASE'!A:R").execute()
```

**Check Outlook inbox:**
```python
requests.get('https://graph.microsoft.com/v1.0/me/messages?$top=20&$orderby=receivedDateTime desc', headers=headers)
```

**Check notifications:**
```python
notifications = db.collection('users').document(user_id).collection('clients').document(client_id).collection('notifications').stream()
```
