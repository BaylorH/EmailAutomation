# Email Automation Test Scenarios

This document describes all test scenarios for the conversation processing and sheet update system.

## How to Run Tests

```bash
# Set your OpenAI API key
export OPENAI_API_KEY='your-key-here'

# Run all tests
cd /Users/baylorharrison/Documents/GitHub/EmailAutomation
python tests/standalone_test.py

# Run specific scenario
python tests/standalone_test.py -s complete_info

# List all scenarios
python tests/standalone_test.py -l

# Save results to file
python tests/standalone_test.py -r results.json
```

---

## Test Scenarios

### 1. `complete_info` - All Information Provided
**Description:** Broker provides all required property information in one reply

**Conversation:**
```
OUT → Hi Scott, I'm interested in 1 Randolph Ct in Evans. Could you provide the property details?

IN ← Hi Jill,

Happy to help! Here are the details for 1 Randolph Ct:

- Total SF: 15,000
- Asking rent: $8.50/SF/yr NNN
- NNN/CAM: $2.25/SF/yr
- 2 drive-in doors
- 4 dock doors
- Clear height: 24 feet
- Power: 400 amps, 3-phase

Let me know if you need anything else!

Best,
Scott
```

**Expected Updates:**
| Column | Value |
|--------|-------|
| Total SF | 15000 |
| Rent/SF /Yr | 8.50 |
| Ops Ex /SF | 2.25 |
| Gross Rent | 10.75 |
| Drive Ins | 2 |
| Docks | 4 |
| Ceiling Ht | 24 |
| Power | 400 amps, 3-phase |

**Expected Events:** None

**Expected Response:** Closing email (all fields complete)

---

### 2. `partial_info` - Needs Follow-up
**Description:** Broker provides only some fields, system should ask for remaining

**Conversation:**
```
OUT → Hi Jeff, interested in 699 Industrial Park Dr. What are the details?

IN ← Hi,

The space is 8,500 SF with asking rent of $6.00/SF NNN.

Jeff
```

**Expected Updates:**
| Column | Value |
|--------|-------|
| Total SF | 8500 |
| Rent/SF /Yr | 6.00 |

**Expected Events:** None

**Expected Response:** Thank you + request for: Ops Ex /SF, Gross Rent, Drive Ins, Docks, Ceiling Ht, Power
- CRITICAL: Must NOT request "Rent/SF /Yr" (already provided)

---

### 3. `property_unavailable` - Property No Longer Available
**Description:** Broker says property is no longer available

**Conversation:**
```
OUT → Hi Luke, following up on 135 Trade Center Court availability.

IN ← Hi Jill,

Unfortunately that property is no longer available - it was leased last week.

Luke
```

**Expected Updates:** None

**Expected Events:** `property_unavailable`

**Expected Response:** Thank you + ask for alternative properties

**Sheet Action:** Row should be moved below NON-VIABLE divider

---

### 4. `unavailable_with_alternative` - Unavailable But Alternative Suggested
**Description:** Property unavailable but broker suggests alternative

**Conversation:**
```
OUT → Hi Scott, is 1 Randolph Ct still available?

IN ← Hi Jill,

Sorry, 1 Randolph Ct is no longer available - we just signed a lease yesterday.

However, I do have another property that might work for you:
456 Commerce Blvd in Martinez - similar size at around 12,000 SF.

Here's the listing: https://example.com/456-commerce

Let me know if you'd like details!

Scott
```

**Expected Updates:** None (for original property)

**Expected Events:**
- `property_unavailable`
- `new_property` (address: 456 Commerce Blvd, city: Martinez)

**Expected Response:** Thank you for both notifications

**Sheet Actions:**
1. Original row moved below NON-VIABLE
2. New row created for 456 Commerce Blvd with pending notification

---

### 5. `call_requested_with_phone` - Call Request With Phone Number
**Description:** Broker requests a call and provides phone number

**Conversation:**
```
OUT → Hi Jeff, following up on 699 Industrial Park Dr.

IN ← Hi Jill,

I'd prefer to discuss this over the phone - there are some details that would be easier to explain.

Can you give me a call at (706) 555-1234?

Thanks,
Jeff
```

**Expected Updates:** None

**Expected Events:** `call_requested` (with phone: (706) 555-1234)

**Expected Response:** NO EMAIL - notification only
- System creates action_needed notification with phone number
- User can see notification in admin UI

---

### 6. `call_requested_no_phone` - Call Request Without Phone Number
**Description:** Broker requests a call but doesn't provide number

**Conversation:**
```
OUT → Hi Luke, checking on 135 Trade Center Court availability.

IN ← Hi,

Can we schedule a call to discuss? I have several options that might work.

Luke
```

**Expected Updates:** None

**Expected Events:** `call_requested`

**Expected Response:** Brief email asking for phone number
- "Could you please provide your phone number so I can give you a call?"

---

### 7. `multi_turn_conversation` - Incremental Data Collection
**Description:** Multiple exchanges gradually filling in data

**Conversation:**
```
OUT → Hi Scott, interested in 1 Randolph Ct. What's the SF and rent?

IN ← Hi Jill, it's 20,000 SF at $7.50/SF NNN. Scott

OUT → Thanks! What are the NNN expenses and dock/door count?

IN ← NNN is $1.85/SF.

We have 3 dock doors and 1 drive-in. Ceiling is 20' clear.

Scott
```

**Expected Updates:**
| Column | Value |
|--------|-------|
| Total SF | 20000 |
| Rent/SF /Yr | 7.50 |
| Ops Ex /SF | 1.85 |
| Gross Rent | 9.35 |
| Docks | 3 |
| Drive Ins | 1 |
| Ceiling Ht | 20 |

**Expected Events:** None

**Expected Response:** Thank you + request for Power (only missing field)

---

### 8. `vague_response` - No Concrete Data
**Description:** Broker gives vague response without concrete numbers

**Conversation:**
```
OUT → Hi Jeff, what's the rent and SF for 699 Industrial Park Dr?

IN ← Hi,

The rent is competitive for the area. It's a nice sized building with good loading.

Let me know if you want to tour.

Jeff
```

**Expected Updates:** None (no concrete data to extract)

**Expected Events:** None

**Expected Response:** Politely re-request specific information
- Should ask for: Total SF, Ops Ex /SF, etc.

---

### 9. `new_property_suggestion` - Additional Property Suggested
**Description:** Broker proactively suggests additional property (original still available)

**Conversation:**
```
OUT → Hi Luke, any updates on 135 Trade Center Court?

IN ← Hi Jill,

135 Trade Center is still available at 25,000 SF.

Also, we just got a new listing you might like:
200 Warehouse Way in North Augusta - 30,000 SF
https://example.com/200-warehouse-way

Both are good options for your client's needs.

Luke
```

**Expected Updates:**
| Column | Value |
|--------|-------|
| Total SF | 25000 |

**Expected Events:** `new_property` (address: 200 Warehouse Way, city: North Augusta)

**Expected Response:** Thank for original info + acknowledge new property

**Sheet Actions:**
- Update original row with Total SF
- Create new row for 200 Warehouse Way with pending notification

---

### 10. `close_conversation` - Natural Conclusion
**Description:** Conversation naturally concludes

**Conversation:**
```
OUT → Thanks for all the info on 135 Trade Center Court!

IN ← You're welcome! Let me know if you need anything else. Good luck with your search!

Luke
```

**Expected Updates:** None

**Expected Events:** `close_conversation`

**Expected Response:** Brief acknowledgment or none

---

## Critical Rules Validated

### 1. Never Request Rent/SF /Yr
The system should NEVER ask for "Rent/SF /Yr" in any response email. This field is provided voluntarily by brokers but should not be explicitly requested.

### 2. PDF Data Trumps Email Body
When a PDF attachment contains property specifications that conflict with the email body, the PDF values should be used (PDFs are more authoritative).

### 3. Human Override Protection
If a human has manually edited a field after AI wrote to it, the AI should not overwrite that value (respects human judgment).

### 4. Auto-Reply Detection
Out of Office and automatic replies should be completely skipped - no processing, no updates, no response.

### 5. Thread Matching
Replies are matched via:
1. In-Reply-To header → msgIndex (primary)
2. References header → msgIndex (secondary)
3. ConversationId → convIndex (fallback)

Only emails that match tracked threads are processed.

---

## Sheet Column Reference

| Column | Description | Format |
|--------|-------------|--------|
| Property Address | Street address | Text |
| City | City name | Text |
| Property Name | Building name | Text |
| Leasing Company | Company name | Text |
| Leasing Contact | Contact name | Text |
| Email | Contact email | Email |
| Total SF | Square footage | Number (no comma) |
| Rent/SF /Yr | Base rent per SF/year | Decimal (e.g., 8.50) |
| Ops Ex /SF | NNN/CAM per SF/year | Decimal |
| Gross Rent | Rent + Ops Ex | Calculated decimal |
| Drive Ins | Drive-in door count | Number |
| Docks | Dock door count | Number |
| Ceiling Ht | Clear height | Number (feet) |
| Power | Electrical specs | Text (e.g., "400A 3-phase") |
| Listing Brokers Comments | Notes from broker | Text |
| Flyer / Link | URLs/Drive links | URLs |
| Floorplan | Floorplan links | URLs |
| Jill and Clients comments | Internal notes | Text |

---

## Expected Pass Rate

A production-ready system should pass **all 10 scenarios** (100% pass rate).

Current known edge cases that may cause issues:
- Conflicting numbers between email and PDF
- Very informal language (requires flexible parsing)
- Multiple properties mentioned in single email
- Partial phone numbers or unusual formats
