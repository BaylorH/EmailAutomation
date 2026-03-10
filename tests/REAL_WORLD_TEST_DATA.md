# Real-World Test Data

Data extracted from actual broker emails forwarded by Jill Ames (mohrpartners.com).

## Source
Emails forwarded to baylor.freelance@outlook.com on 2025-08-25 and 2025-05-21.

## Test Scenarios

### 1. 135 Trade Center Court, Augusta, GA
**File:** `conversations/real_world_135_trade_center.txt`
**Broker:** Luke Coffey (luke.coffey@southeastern.company)
**Scenario:** Complete info with call offer and multiple PDFs

**What to test:**
- Data extraction: $15/SF/NNN rent, July 2025 delivery
- Call offer detection (should escalate)
- PDF categorization:
  - `Sealed Bldg C 10-24-23.pdf` → Floorplan column
  - `Sealed Bldg D 10-24-23.pdf` → Floorplan column
  - `135 Trade Center Court - Brochure.pdf` → Flyer/Link column

**PDFs:** `test_pdfs/real_world/`

---

### 2. The Tapestry - 9300 Lottsford Rd, Largo MD
**File:** `conversations/real_world_tapestry_confidential.txt`
**Broker:** Craig Cheney (ccheney@KLNB.com)
**Scenario:** Confidentiality question - MUST escalate

**What to test:**
- Broker asks: "what franchise is it that you are working with?"
- MUST trigger `needs_user_input:confidential`
- System should NOT auto-reply
- PDF categorization: `Tapestry Largo Station Retail Floor Plan.pdf` → Floorplan column

**Critical:** Jill asked "Can the A.I. Auto respond to that?" - Answer is NO, it escalates.

---

### 3. Woodmore Commons - 2017 St. Josephs Drive, Bowie, MD
**File:** `conversations/real_world_woodmore_unavailable.txt`
**Broker:** Brian Greene (bg@hp-llc.com)
**Scenario:** Property unavailable + new property suggestion

**What to test:**
- Detect "already at lease" + "our last one" → `property_unavailable`
- Move row below NON-VIABLE divider
- Detect new property suggestion: The Centre at Forestville
- URL provided: https://www.hp-llc.com/the-centre-at-forestville
- Should create approval notification for new property

**Jill's question:** "Can A.I. Look at the website and scan for any of the data?"
- This is a future feature (web scraping for property data)

---

## Using Real-World PDFs in Tests

```python
# Copy real-world PDFs to simulate email attachments
import shutil
src = "test_pdfs/real_world/Sealed Bldg C 10-24-23.pdf"
dst = "/tmp/test_attachment.pdf"
shutil.copy(src, dst)

# Test floorplan detection
from email_automation.sheets import is_floorplan_filename
assert is_floorplan_filename("Sealed Bldg C 10-24-23.pdf") == True
assert is_floorplan_filename("135 Trade Center Court - Brochure.pdf") == False
```

---

## Floorplan Detection Patterns

The following patterns categorize a file as a floorplan (vs flyer):
- "floor plan", "floorplan", "floor-plan", "floor_plan"
- "layout"
- "site plan", "siteplan", "site-plan", "site_plan"
- "sealed" (sealed architectural drawings)
- "blueprint"
- "bldg" (building abbreviation)
- "building plan"

All other PDFs go to the Flyer/Link column.

---

## Integration Test Checklist

- [ ] Floorplan detection correctly categorizes all 4 PDFs
- [ ] Confidentiality questions trigger escalation (not auto-reply)
- [ ] Property unavailable moves row below NON-VIABLE
- [ ] New property suggestions create approval notifications
- [ ] Call offers trigger escalation
- [ ] Separate Flyer and Floorplan columns get correct PDFs
