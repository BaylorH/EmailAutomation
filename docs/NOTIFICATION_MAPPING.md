# Notification System Mapping

## Current Notification Types

| Kind | Reason/Subtype | Trigger | Priority | Frontend Support |
|------|----------------|---------|----------|------------------|
| `sheet_update` | - | AI extracts field from email | normal | Full |
| `row_completed` | - | All required fields complete | important | Full |
| `property_unavailable` | - | Broker says property leased/unavailable | important | Full |
| `action_needed` | `call_requested` | Broker requests phone call | important | Full |
| `action_needed` | `new_property_pending_send` | Broker suggests new property | important | Full |
| `action_needed` | `missing_fields` | (Legacy - not currently used) | important | Partial |
| `action_needed` | `needs_user_input:client_question` | Broker asks about client requirements | important | **MISSING** |
| `action_needed` | `needs_user_input:scheduling` | Tour/meeting scheduling request | important | **MISSING** |
| `action_needed` | `needs_user_input:negotiation` | Price/term negotiation | important | **MISSING** |
| `action_needed` | `needs_user_input:confidential` | Asks who client is | important | **MISSING** |
| `action_needed` | `needs_user_input:legal_contract` | LOI/contract questions | important | **MISSING** |
| `action_needed` | `needs_user_input:unclear` | Message needs review | important | **MISSING** |
| `action_needed` | `contact_optout:not_interested` | Contact not interested | important | **MISSING** |
| `action_needed` | `contact_optout:unsubscribe` | Requested removal | important | **MISSING** |
| `action_needed` | `contact_optout:do_not_contact` | Firm no contact request | important | **MISSING** |
| `action_needed` | `contact_optout:no_tenant_reps` | Won't work with tenant reps | important | **MISSING** |
| `action_needed` | `contact_optout:direct_only` | Only deals direct with tenants | important | **MISSING** |
| `action_needed` | `contact_optout:hostile` | Negative/hostile response | important | **MISSING** |
| `action_needed` | `wrong_contact:no_longer_handles` | Contact doesn't handle property anymore | important | **MISSING** |
| `action_needed` | `wrong_contact:wrong_person` | Wrong contact entirely | important | **MISSING** |
| `action_needed` | `wrong_contact:forwarded` | Being forwarded to right person | important | **MISSING** |
| `action_needed` | `wrong_contact:left_company` | Contact left company | important | **MISSING** |

---

## Test Scenarios → Expected Notifications

| Test Scenario | Events Detected | Notifications That SHOULD Fire | Currently Tested? |
|---------------|-----------------|--------------------------------|-------------------|
| `complete_info` | (none) | `sheet_update` x7, `row_completed` x1 | Events: Yes, Notifications: **NO** |
| `partial_info` | (none) | `sheet_update` x2 | Events: Yes, Notifications: **NO** |
| `property_unavailable` | `property_unavailable` | `property_unavailable` x1 | Events: Yes, Notifications: **NO** |
| `unavailable_with_alternative` | `property_unavailable`, `new_property` | `property_unavailable` x1, `action_needed:new_property_pending_send` x1 | Events: Yes, Notifications: **NO** |
| `call_requested_with_phone` | `call_requested` | `action_needed:call_requested` x1 (with phone) | Events: Yes, Notifications: **NO** |
| `call_requested_no_phone` | `call_requested` | `action_needed:call_requested` x1 (no phone) | Events: Yes, Notifications: **NO** |
| `multi_turn_conversation` | (none) | `sheet_update` x6 | Events: Yes, Notifications: **NO** |
| `vague_response` | `needs_user_input` | `action_needed:needs_user_input:scheduling` x1 | Events: Yes, Notifications: **NO** |
| `new_property_suggestion` | `new_property` | `sheet_update` x1, `action_needed:new_property_pending_send` x1 | Events: Yes, Notifications: **NO** |
| `close_conversation` | `close_conversation` | (no notification - just logged) | Events: Yes, Notifications: N/A |
| `client_asks_requirements` | `needs_user_input` | `action_needed:needs_user_input:client_question` x1 | Events: Yes, Notifications: **NO** |
| `scheduling_request` | `needs_user_input` | `action_needed:needs_user_input:scheduling` x1 | Events: Yes, Notifications: **NO** |
| `negotiation_attempt` | `needs_user_input` | `action_needed:needs_user_input:negotiation` x1 | Events: Yes, Notifications: **NO** |
| `identity_question` | `needs_user_input` | `action_needed:needs_user_input:confidential` x1 | Events: Yes, Notifications: **NO** |
| `legal_contract_question` | `needs_user_input` | `action_needed:needs_user_input:legal_contract` x1 | Events: Yes, Notifications: **NO** |
| `mixed_info_and_question` | `needs_user_input` | `sheet_update` x4, `action_needed:needs_user_input:client_question` x1 | Events: Yes, Notifications: **NO** |
| `budget_question` | `needs_user_input` | `action_needed:needs_user_input:client_question` x1 | Events: Yes, Notifications: **NO** |

### Key Finding: Tests validate AI events but NOT actual notification creation

The test suite validates that `propose_sheet_updates()` returns the correct `events` array, but it doesn't test that `processing.py` correctly converts those events into Firestore notifications.

---

## Frontend Gaps

### Currently Supported (has icon + title + description)
- `sheet_update` → "Updated: {column}"
- `row_completed` → "Property Complete"
- `property_unavailable` → "Property Unavailable"
- `action_needed:call_requested` → "Call Requested"
- `action_needed:new_property_pending_send` → "New Property Request"
- `action_needed:missing_fields` → "Missing Information"

### Missing Frontend Support (falls back to generic)
```javascript
// These all fall through to default case:
case 'action_needed':
  if (n.meta?.reason === 'call_requested') return 'Call Requested';
  if (n.meta?.reason === 'new_property_pending_send') return 'New Property Request';
  if (n.meta?.reason === 'missing_fields') return 'Missing Information';
  return 'Action Needed';  // <-- All needs_user_input, contact_optout, wrong_contact go here
```

### Needed Frontend Updates

```javascript
// NotificationsSidebar.jsx - getNotificationTitle()
case 'action_needed':
  if (n.meta?.reason === 'call_requested') return 'Call Requested';
  if (n.meta?.reason === 'new_property_pending_send') return 'New Property Request';
  if (n.meta?.reason === 'missing_fields') return 'Missing Information';

  // ADD THESE:
  if (n.meta?.reason?.startsWith('needs_user_input:')) {
    const subReason = n.meta.reason.split(':')[1];
    switch (subReason) {
      case 'client_question': return 'Client Question';
      case 'scheduling': return 'Scheduling Request';
      case 'negotiation': return 'Negotiation';
      case 'confidential': return 'Confidential Question';
      case 'legal_contract': return 'Contract/LOI Request';
      default: return 'Needs Review';
    }
  }
  if (n.meta?.reason?.startsWith('contact_optout:')) return 'Contact Opted Out';
  if (n.meta?.reason?.startsWith('wrong_contact:')) return 'Wrong Contact';

  return 'Action Needed';
```

---

## Suggested New Notifications

### 1. `email_sent` - Track outbound emails
**Why:** User should know when system sends emails on their behalf
```javascript
{
  kind: "email_sent",
  priority: "normal",
  meta: {
    emailType: "initial_inquiry" | "follow_up" | "closing" | "new_property_request",
    recipient: "broker@email.com",
    subject: "..."
  }
}
```

### 2. `email_failed` - Alert on send failures
**Why:** Critical to know if emails aren't being delivered
```javascript
{
  kind: "email_failed",
  priority: "important",
  meta: {
    reason: "bounce" | "invalid_address" | "api_error",
    recipient: "...",
    errorMessage: "..."
  }
}
```

### 3. `ai_extraction_low_confidence` - Flag uncertain extractions
**Why:** User should review extractions the AI wasn't confident about
```javascript
{
  kind: "action_needed",
  meta: {
    reason: "low_confidence_extraction",
    column: "Power",
    value: "200A maybe?",
    confidence: 0.45,
    originalText: "..."
  }
}
```

### 4. `thread_stale` - No response in X days
**Why:** Alert user to follow up on unanswered threads
```javascript
{
  kind: "action_needed",
  meta: {
    reason: "thread_stale",
    daysSinceLastMessage: 7,
    lastMessageDate: "...",
    suggestedAction: "Send follow-up?"
  }
}
```

### 5. `suggested_response_ready` - AI has draft response
**Why:** When escalating (needs_user_input), include suggested response user can edit/send
```javascript
{
  kind: "action_needed",
  meta: {
    reason: "needs_user_input:scheduling",
    question: "Can you tour Tuesday at 2pm?",
    suggestedResponse: "Hi Scott, Let me check with my client on availability and get back to you...",
    canAutoSend: false  // Requires user approval
  }
}
```

### 6. `duplicate_property_detected` - Potential duplicate
**Why:** Alert if broker mentions property that might already exist in sheet
```javascript
{
  kind: "action_needed",
  meta: {
    reason: "potential_duplicate",
    newAddress: "123 Main St",
    existingAddress: "123 Main Street",  // Slight variation
    existingRow: 5,
    similarity: 0.95
  }
}
```

### 7. `batch_summary` - Daily/weekly digest
**Why:** Summary notification instead of many individual ones
```javascript
{
  kind: "batch_summary",
  priority: "normal",
  meta: {
    period: "daily",
    sheetUpdates: 15,
    actionsNeeded: 3,
    propertiesCompleted: 2,
    propertiesUnavailable: 1
  }
}
```

---

## Meta Fields Currently Stored

### `sheet_update`
```javascript
meta: {
  column: "Total SF",
  oldValue: "",
  newValue: "15000",
  reason: "Broker stated: 'Total SF: 15,000'",
  confidence: 0.95,
  address: "1 Randolph Ct, Evans"
}
```

### `action_needed:call_requested`
```javascript
meta: {
  reason: "call_requested",
  details: "Call requested - phone number provided: (706) 555-1234",
  phoneNumber: "(706) 555-1234"  // Optional
}
```

### `action_needed:needs_user_input:*`
```javascript
meta: {
  reason: "needs_user_input:scheduling",
  details: "Tour/meeting scheduling request",
  question: "Can you come by Tuesday at 2pm?",
  originalMessage: "Hi Jill, Great! Can you come by..."  // First 500 chars
}
```

### `action_needed:new_property_pending_send`
```javascript
meta: {
  reason: "new_property_pending_send",
  status: "pending_send",
  address: "456 Commerce Blvd",
  city: "Martinez",
  link: "https://example.com/listing",
  notes: "12,000 SF, similar to original",
  suggestedEmail: {
    to: "broker@email.com",
    subject: "RE: 456 Commerce Blvd, Martinez",
    body: "Hi Scott, You mentioned a new property..."
  }
}
```

### `action_needed:contact_optout:*`
```javascript
meta: {
  reason: "contact_optout:not_interested",
  details: "Contact is not interested",
  contact: "broker@email.com",
  originalMessage: "Not interested, please remove me..."
}
```

### `action_needed:wrong_contact:*`
```javascript
meta: {
  reason: "wrong_contact:forwarded",
  details: "Message being forwarded to correct person. Suggested contact: John Smith (john@broker.com)",
  originalContact: "old@broker.com",
  suggestedContact: "John Smith",
  suggestedEmail: "john@broker.com",
  suggestedPhone: "(555) 123-4567",
  originalMessage: "I don't handle that property anymore..."
}
```

### `property_unavailable`
```javascript
meta: {
  address: "135 Trade Center Court",
  city: "Augusta"
}
```

### `row_completed`
```javascript
meta: {
  completedFields: ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"],
  missingFields: []
}
```

---

## Recommended Actions

### Immediate (Bug Fixes)
1. **Frontend:** Add handlers for `needs_user_input:*`, `contact_optout:*`, `wrong_contact:*`
2. **Frontend:** Show `question` field in notification details for `needs_user_input`
3. **Frontend:** Show `suggestedEmail` for `new_property_pending_send` with send/edit buttons

### Short Term (Robustness)
4. **Backend:** Add `email_sent` notification when emails are sent
5. **Backend:** Add `email_failed` notification on send errors
6. **Tests:** Add notification validation to test suite (not just events)

### Medium Term (Features)
7. **Backend:** Implement `thread_stale` detection in scheduler
8. **Backend:** Add `suggested_response` to `needs_user_input` notifications
9. **Frontend:** Add inline response composer for escalated notifications

### Long Term (Nice to Have)
10. **Backend:** Implement `batch_summary` for daily digests
11. **Backend:** Add `duplicate_property_detected` with fuzzy matching
12. **Frontend:** Notification preferences (which types to show/hide)
