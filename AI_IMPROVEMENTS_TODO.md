# AI Processing Improvements

## Issue: Event Detection Working but Processing Failing

The AI (GPT-5.2) correctly detects events like `property_unavailable` and `new_property`, but the backend event processing can fail silently, causing:
- Rows not moved to NON-VIABLE
- Notifications not created
- Wrong code path sending AI responses

## Proposed Fixes

### 1. Remove Redundant Keyword Verification
**File:** `email_automation/processing.py` (lines 1136-1148)

The AI already detected the event - we shouldn't second-guess it with keyword matching.

```python
# BEFORE: Requires keyword match even after AI detection
if any(keyword in message_content for keyword in unavailable_keywords):
    # process event

# AFTER: Trust the AI detection
# (optionally log for debugging)
print(f"🔍 AI detected property_unavailable, proceeding with event handling")
# process event
```

### 2. Add Response-Scenario Validation
Don't use LLM response if it doesn't match the current code path.

```python
# Before sending LLM response, validate it matches the scenario
if old_row_became_nonviable or new_row_created:
    # LLM response designed for this scenario
    response_body = llm_response_email
else:
    # Don't use LLM response meant for different scenario
    # Generate appropriate template response instead
    response_body = generate_template_response(missing_fields, contact_name)
```

### 3. Add Event Processing Logging
Better visibility into why events fail.

```python
for event in events:
    event_type = event.get("type")
    print(f"🔄 Processing event: {event_type}")

    try:
        # ... process event
        print(f"✅ Event processed: {event_type}")
    except Exception as e:
        print(f"❌ Event failed: {event_type} - {e}")
        # Don't silently continue - flag for review
```

### 4. Consider Model Improvements

**Current:** GPT-5.2 for extraction
**Issue:** Response mixed up property names ("150 Trade Center" vs "150 Trade Center Court")

Options:
- Use Claude for complex reasoning/response generation
- Use separate model calls: one for extraction, one for response
- Add property name verification in response generation

### 5. Prevent Wrong Code Path

Add a flag to track which scenario the AI response was designed for:

```python
proposal["response_scenario"] = "property_unavailable_with_new_property"

# Later, when sending response:
if proposal.get("response_scenario") != current_scenario:
    print(f"⚠️ LLM response designed for different scenario, using template")
    response_body = template_response
```

## Priority Order

1. **Remove keyword verification** - Quick fix, high impact
2. **Add logging** - Essential for debugging
3. **Response-scenario validation** - Prevents wrong responses being sent
4. **Model improvements** - Longer term optimization
