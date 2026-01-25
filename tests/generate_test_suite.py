#!/usr/bin/env python3
"""
Test Suite Generator
====================
Generates hundreds of test cases across all scenario categories.

Usage:
    python tests/generate_test_suite.py --output tests/generated_suite/
    python tests/generate_test_suite.py --list-categories
    python tests/generate_test_suite.py --category response_types --count 50
"""

import os
import sys
import json
import random
import argparse
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

# ============================================================================
# TEST PROPERTY DATA
# ============================================================================

CITIES = [
    ("Augusta", "GA"), ("Evans", "GA"), ("Martinez", "GA"),
    ("North Augusta", "SC"), ("Aiken", "SC"), ("Grovetown", "GA"),
    ("Columbia", "SC"), ("Greenville", "SC"), ("Atlanta", "GA"),
    ("Savannah", "GA")
]

STREET_NAMES = [
    "Industrial", "Commerce", "Warehouse", "Distribution", "Logistics",
    "Trade", "Enterprise", "Business", "Corporate", "Technology",
    "Manufacturing", "Gateway", "Parkway", "Center", "Park"
]

STREET_TYPES = ["Way", "Blvd", "Dr", "Ct", "Pkwy", "Rd", "St", "Ave", "Ln", "Circle"]

FIRST_NAMES = [
    "John", "Sarah", "Mike", "Lisa", "Tom", "Emily", "David", "Jennifer",
    "Chris", "Amanda", "James", "Michelle", "Robert", "Jessica", "William",
    "Ashley", "Michael", "Stephanie", "Daniel", "Nicole", "Matthew", "Laura",
    "Andrew", "Rachel", "Joshua", "Megan", "Brandon", "Heather", "Kevin", "Amy"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas",
    "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris", "Clark"
]

BROKER_COMPANIES = [
    "CBRE", "JLL", "Cushman & Wakefield", "Colliers", "NAI", "Marcus & Millichap",
    "Berkshire Hathaway", "Coldwell Banker", "RE/MAX Commercial", "Keller Williams",
    "Lincoln Property", "Prologis", "Duke Realty", "Liberty Property", "DCT Industrial"
]


def generate_property(index: int) -> Dict:
    """Generate a random test property."""
    street_num = random.randint(100, 9999)
    street_name = random.choice(STREET_NAMES)
    street_type = random.choice(STREET_TYPES)
    city, state = random.choice(CITIES)

    first_name = random.choice(FIRST_NAMES)
    last_name = random.choice(LAST_NAMES)
    company = random.choice(BROKER_COMPANIES)

    email_domain = company.lower().replace(" ", "").replace("&", "")[:10] + ".com"
    email = f"{first_name.lower()}.{last_name.lower()}@{email_domain}"

    return {
        "id": f"prop_{index:04d}",
        "address": f"{street_num} {street_name} {street_type}",
        "city": city,
        "state": state,
        "contact": f"{first_name} {last_name}",
        "company": company,
        "email": email,
        "rowIndex": index + 3
    }


def generate_property_data() -> Dict:
    """Generate random property specifications."""
    return {
        "sf": random.choice([5000, 8000, 10000, 12000, 15000, 18000, 20000, 25000, 30000, 40000, 50000]),
        "rent": round(random.uniform(4.0, 12.0), 2),
        "opex": round(random.uniform(1.5, 3.5), 2),
        "docks": random.randint(0, 8),
        "driveins": random.randint(0, 4),
        "ceiling": random.choice([18, 20, 22, 24, 26, 28, 30, 32]),
        "power": random.choice([
            "200 amps", "400 amps", "600 amps", "800 amps",
            "200 amps, single-phase", "400 amps, 3-phase",
            "600 amps, 3-phase", "800 amps, 3-phase", "1000 amps, 3-phase"
        ]),
        "availability": random.choice([
            "immediately", "in 30 days", "March 1st", "Q2 2026",
            "upon lease signing", "flexible"
        ])
    }


# ============================================================================
# RESPONSE TEMPLATES
# ============================================================================

COMPLETE_INFO_TEMPLATES = [
    # Formal
    """Dear {name},

Thank you for your inquiry regarding {address}. I'm pleased to provide the following details:

Total Size: {sf:,} SF
Asking Rate: ${rent}/SF/yr NNN
Operating Expenses: ${opex}/SF/yr
Loading: {docks} dock-high doors, {driveins} drive-in doors
Clear Height: {ceiling}'
Electrical: {power}

The space is available {availability}. Please let me know if you'd like to schedule a tour.

Best regards,
{contact}""",

    # Casual
    """Hey {name},

Yeah {address} is still available! Here's what we've got:

- {sf:,} SF
- ${rent}/SF NNN
- ${opex} CAM
- {docks} docks + {driveins} grade doors
- {ceiling}' clear
- {power}

Available {availability}. Want to take a look?

{contact}""",

    # Bullet points
    """Hi {name},

Here are the specs for {address}:

• Total SF: {sf:,}
• Rent: ${rent}/SF/yr NNN
• NNN/CAM: ${opex}/SF
• Dock Doors: {docks}
• Drive-Ins: {driveins}
• Ceiling Height: {ceiling}'
• Power: {power}

Available {availability}.

Thanks,
{contact}""",

    # Table-like
    """{name},

{address} details:

Square Footage:    {sf:,} SF
Asking Rent:       ${rent}/SF NNN
Expenses:          ${opex}/SF
Docks:             {docks}
Drive-Ins:         {driveins}
Clear Height:      {ceiling} ft
Power:             {power}
Availability:      {availability}

{contact}""",

    # Narrative
    """Hi {name},

Thanks for reaching out about {address}. This is a great space - {sf:,} square feet with {ceiling}' clear height. We're asking ${rent} per foot NNN with about ${opex} in operating expenses. The building has {docks} dock doors and {driveins} drive-in doors, with {power} available.

It's available {availability}. Let me know if you have any questions!

{contact}"""
]

PARTIAL_INFO_TEMPLATES = [
    """Hi {name},

The space at {address} is {sf:,} SF with asking rent of ${rent}/SF NNN.

Let me know if you need anything else.

{contact}""",

    """{name},

Quick answer on {address} - it's {sf:,} SF available {availability}. Asking ${rent}/SF.

{contact}""",

    """Hi,

{address}: {sf:,} SF, {docks} dock doors.

Happy to provide more details if interested.

{contact}"""
]

PROPERTY_UNAVAILABLE_TEMPLATES = [
    """Hi {name},

Unfortunately {address} is no longer available - we just signed a lease last week.

If anything else comes up in the area I'll let you know.

{contact}""",

    """{name},

Sorry, {address} was leased. It went quick!

I'll keep you posted if something similar becomes available.

{contact}""",

    """Hi,

That property is under contract. Should close within 30 days.

{contact}"""
]

NEW_PROPERTY_SAME_CONTACT_TEMPLATES = [
    """Hi {name},

{address} is available at {sf:,} SF for ${rent}/SF.

I also have another property that might work - {new_address} in {new_city}. Similar size, about {new_sf:,} SF. Here's the listing: https://example.com/{new_address_slug}

Let me know if you want details on either.

{contact}""",

    """{name},

Yes on {address}. Also, check out {new_address} - it just came on the market. {new_sf:,} SF, great loading.

{contact}"""
]

NEW_PROPERTY_DIFF_CONTACT_TEMPLATES = [
    """Hi {name},

I can help with {address}, but you should also reach out to {new_contact} at {new_email} about {new_address} - it's a great option too.

{contact}""",

    """{name},

For {address}, I'm your guy. But my colleague {new_contact} ({new_email}) has {new_address} which might be perfect. Tell {new_contact_first} I sent you.

{contact}"""
]

ESCALATION_TEMPLATES = {
    "identity_question": [
        """Hi {name},

Before I send over the details on {address}, can you tell me who your client is? What company are they with?

{contact}""",
        """Who is your client exactly? I like to know who I'm working with.

{contact}"""
    ],
    "budget_question": [
        """Hi {name},

The property at {address} is {sf:,} SF with {ceiling}' clear.

What's the budget range your client is working with? That'll help me know if this is a good fit.

{contact}""",
        """{name},

What kind of budget are we looking at? Don't want to waste your time if the numbers don't work.

{contact}"""
    ],
    "size_question": [
        """Hi {name},

Before I send the details, what size space does your client need? And what's their timeline for moving in?

{contact}""",
        """How much space do they actually need? {address} is {sf:,} SF - is that the right range?

{contact}"""
    ],
    "negotiation": [
        """Hi {name},

Regarding {address} - the landlord is firm at ${rent}/SF, but if your client can commit to a 5-year term instead of 3, they could potentially do ${lower_rent}/SF. Would they consider that?

{contact}""",
        """{name},

Would your client consider ${lower_rent}/SF if they signed a longer lease? Just trying to make a deal work here.

{contact}"""
    ],
    "tour_offer": [
        """Hi {name},

{address} is available. Would you like to schedule a tour? I'm free Tuesday at 2pm or Wednesday morning.

{contact}""",
        """Want to see {address}? I can meet you there tomorrow afternoon.

{contact}"""
    ],
    "call_request_with_phone": [
        """Hi {name},

I'd prefer to discuss {address} over the phone - there are some details that would be easier to explain.

Can you call me at {phone}?

{contact}""",
        """Let's hop on a call about this one. My cell is {phone}.

{contact}"""
    ],
    "call_request_no_phone": [
        """Hi {name},

Can we schedule a call to discuss? I have several options that might work for your client.

{contact}""",
        """This is easier to discuss live. When can you talk?

{contact}"""
    ],
    "contract_request": [
        """Hi {name},

If your client is ready to move forward, can you send over an LOI with your proposed terms? We'll need the lease term, preferred start date, and any TI requirements.

{contact}""",
        """Ready to make a deal? Send me an LOI and we'll go from there.

{contact}"""
    ]
}

EDGE_CASE_TEMPLATES = {
    "hostile": [
        """Not interested. Stop emailing me.

Don't contact me again.""",
        """We don't work with tenant reps. Find another broker."""
    ],
    "out_of_office": [
        """Thank you for your email. I am currently out of the office with limited access to email. I will return on Monday, January 27th.

For urgent matters, please contact our office at 555-123-4567.

Best,
{contact}""",
        """I'm OOO until next week. Will respond then.

{contact}"""
    ],
    "forward_to_colleague": [
        """Hi {name},

I don't handle that property anymore - forwarding your email to {new_contact} who can help.

{contact}""",
        """That's {new_contact}'s listing now. CCing them here."""
    ],
    "wrong_person": [
        """{name},

I think you have the wrong person - I don't have anything at {address}. Try reaching out to {new_contact} at {new_company}.

{contact}""",
        """Wrong broker - that's not my listing."""
    ],
    "very_short": [
        """Yes.""",
        """No.""",
        """Available.""",
        """Leased.""",
        """Call me."""
    ],
    "mixed_info_question": [
        """Hi {name},

The space at {address} is {sf:,} SF with {ceiling}' clear height. We have {docks} docks and {driveins} drive-in.

By the way, what's your client's budget? And do they need heavy power or standard?

{contact}"""
    ],
    "property_issue": [
        """Hi {name},

{address} is available - {sf:,} SF for ${rent}/SF.

Fair warning though, there's been some water damage in the back corner that the landlord is addressing. Should be fixed within 2 weeks.

{contact}""",
        """Just so you know, the HVAC system is old and probably needs replacement within a year. Landlord might do TI credit for it.

{contact}"""
    ]
}


# ============================================================================
# TEST CASE GENERATION
# ============================================================================

@dataclass
class TestCase:
    """A single test case."""
    id: str
    category: str
    type: str
    property: Dict
    conversation: List[Dict]
    expected: Dict
    forbidden: Dict
    metadata: Dict


def generate_complete_info_test(prop: Dict, index: int) -> TestCase:
    """Generate a complete_info test case."""
    data = generate_property_data()
    template = random.choice(COMPLETE_INFO_TEMPLATES)

    broker_response = template.format(
        name="Jill",
        address=prop["address"],
        contact=prop["contact"].split()[0],
        **data
    )

    return TestCase(
        id=f"R01_complete_info_{index:03d}",
        category="response_types",
        type="complete_info",
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi {prop['contact'].split()[0]}, I'm interested in {prop['address']}. Could you provide availability and details?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected={
            "updates": [
                {"column": "Total SF", "value": str(data["sf"])},
                {"column": "Rent/SF /Yr", "value": str(data["rent"])},
                {"column": "Ops Ex /SF", "value": str(data["opex"])},
                {"column": "Docks", "value": str(data["docks"])},
                {"column": "Drive Ins", "value": str(data["driveins"])},
                {"column": "Ceiling Ht", "value": str(data["ceiling"])},
                {"column": "Power", "value": data["power"]}
            ],
            "events": [],
            "row_complete": True,
            "response_type": "closing"
        },
        forbidden={
            "updates": ["Leasing Contact", "Email", "Gross Rent", "Property Address", "City"],
            "requests": ["Rent/SF /Yr", "Gross Rent"]
        },
        metadata={"data": data}
    )


def generate_partial_info_test(prop: Dict, index: int) -> TestCase:
    """Generate a partial_info test case."""
    data = generate_property_data()
    template = random.choice(PARTIAL_INFO_TEMPLATES)

    broker_response = template.format(
        name="Jill",
        address=prop["address"],
        contact=prop["contact"].split()[0],
        **data
    )

    return TestCase(
        id=f"R02_partial_info_{index:03d}",
        category="response_types",
        type="partial_info",
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi {prop['contact'].split()[0]}, I'm interested in {prop['address']}. What are the details?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected={
            "updates": [
                {"column": "Total SF", "value": str(data["sf"])}
            ],
            "events": [],
            "row_complete": False,
            "response_type": "request_missing"
        },
        forbidden={
            "updates": ["Leasing Contact", "Email", "Gross Rent"],
            "requests": ["Rent/SF /Yr", "Gross Rent"]
        },
        metadata={"data": data}
    )


def generate_unavailable_test(prop: Dict, index: int) -> TestCase:
    """Generate a property_unavailable test case."""
    template = random.choice(PROPERTY_UNAVAILABLE_TEMPLATES)

    broker_response = template.format(
        name="Jill",
        address=prop["address"],
        contact=prop["contact"].split()[0]
    )

    return TestCase(
        id=f"R05_unavailable_{index:03d}",
        category="response_types",
        type="property_unavailable",
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi {prop['contact'].split()[0]}, is {prop['address']} still available?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected={
            "updates": [],
            "events": [{"type": "property_unavailable"}],
            "row_complete": False,
            "response_type": "unavailable"
        },
        forbidden={
            "updates": ["Leasing Contact", "Email", "Gross Rent"],
            "requests": []
        },
        metadata={}
    )


def generate_escalation_test(prop: Dict, escalation_type: str, index: int) -> TestCase:
    """Generate an escalation test case."""
    data = generate_property_data()
    templates = ESCALATION_TEMPLATES.get(escalation_type, [])
    if not templates:
        templates = ESCALATION_TEMPLATES["budget_question"]

    template = random.choice(templates)

    # Add extra format vars for some templates
    format_vars = {
        "name": "Jill",
        "address": prop["address"],
        "contact": prop["contact"].split()[0],
        "phone": f"555-{random.randint(100,999)}-{random.randint(1000,9999)}",
        "lower_rent": round(data["rent"] - 1.0, 2),
        **data
    }

    broker_response = template.format(**format_vars)

    # Determine expected event
    event_map = {
        "identity_question": ("needs_user_input", "confidential"),
        "budget_question": ("needs_user_input", "client_question"),
        "size_question": ("needs_user_input", "client_question"),
        "negotiation": ("needs_user_input", "negotiation"),
        "tour_offer": ("tour_requested", None),
        "call_request_with_phone": ("call_requested", None),
        "call_request_no_phone": ("call_requested", None),
        "contract_request": ("needs_user_input", "legal_contract")
    }

    event_type, reason = event_map.get(escalation_type, ("needs_user_input", "unknown"))

    expected_event = {"type": event_type}
    if reason:
        expected_event["reason"] = reason

    # call_request_no_phone should have response email asking for phone
    # other escalations should have no response email
    should_have_response = escalation_type == "call_request_no_phone"

    return TestCase(
        id=f"E_{escalation_type}_{index:03d}",
        category="escalations",
        type=escalation_type,
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi {prop['contact'].split()[0]}, I'm interested in {prop['address']}. Can you send the details?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected={
            "updates": [],
            "events": [expected_event],
            "row_complete": False,
            # call_request_no_phone should ask for phone; others should escalate
            "response_email": "ask_for_phone" if should_have_response else None
        },
        forbidden={
            "updates": ["Leasing Contact", "Email"],
            "requests": []
        },
        metadata={"escalation_type": escalation_type}
    )


def generate_edge_case_test(prop: Dict, edge_type: str, index: int) -> TestCase:
    """Generate an edge case test."""
    data = generate_property_data()
    templates = EDGE_CASE_TEMPLATES.get(edge_type, EDGE_CASE_TEMPLATES["very_short"])
    template = random.choice(templates)

    format_vars = {
        "name": "Jill",
        "address": prop["address"],
        "contact": prop["contact"].split()[0],
        "new_contact": random.choice(FIRST_NAMES) + " " + random.choice(LAST_NAMES),
        "new_company": random.choice(BROKER_COMPANIES),
        "new_email": f"{random.choice(FIRST_NAMES).lower()}@broker.com",
        **data
    }

    broker_response = template.format(**format_vars)

    # Determine expected behavior
    expected = {"updates": [], "events": [], "row_complete": False}

    if edge_type == "hostile":
        expected["events"] = [{"type": "contact_optout"}]
    elif edge_type == "out_of_office":
        expected["response_type"] = "wait_or_follow_up"
    elif edge_type in ["forward_to_colleague", "wrong_person"]:
        expected["events"] = [{"type": "wrong_contact"}]
    elif edge_type == "property_issue":
        expected["events"] = [{"type": "property_issue"}]
    elif edge_type == "mixed_info_question":
        expected["events"] = [{"type": "needs_user_input"}]
        expected["updates"] = [{"column": "Total SF", "value": str(data["sf"])}]

    return TestCase(
        id=f"X_{edge_type}_{index:03d}",
        category="edge_cases",
        type=edge_type,
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi, I'm interested in {prop['address']}. Can you provide details?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected=expected,
        forbidden={
            "updates": ["Leasing Contact", "Email", "Gross Rent"],
            "requests": []
        },
        metadata={"edge_type": edge_type}
    )


def generate_new_property_test(prop: Dict, diff_contact: bool, index: int) -> TestCase:
    """Generate a new_property test case."""
    data = generate_property_data()
    new_prop = generate_property(1000 + index)
    new_data = generate_property_data()

    if diff_contact:
        templates = NEW_PROPERTY_DIFF_CONTACT_TEMPLATES
        test_type = "new_property_diff_contact"
    else:
        templates = NEW_PROPERTY_SAME_CONTACT_TEMPLATES
        test_type = "new_property_same_contact"

    template = random.choice(templates)

    format_vars = {
        "name": "Jill",
        "address": prop["address"],
        "contact": prop["contact"].split()[0],
        "new_address": new_prop["address"],
        "new_city": new_prop["city"],
        "new_sf": new_data["sf"],
        "new_contact": new_prop["contact"],
        "new_contact_first": new_prop["contact"].split()[0],
        "new_email": new_prop["email"],
        "new_address_slug": new_prop["address"].lower().replace(" ", "-"),
        **data
    }

    broker_response = template.format(**format_vars)

    expected_event = {
        "type": "new_property",
        "address": new_prop["address"]
    }
    if diff_contact:
        expected_event["contactName"] = new_prop["contact"].split()[0]
        expected_event["email"] = new_prop["email"]

    return TestCase(
        id=f"R_{'07' if not diff_contact else '08'}_{test_type}_{index:03d}",
        category="response_types",
        type=test_type,
        property=prop,
        conversation=[
            {
                "direction": "outbound",
                "content": f"Hi {prop['contact'].split()[0]}, is {prop['address']} available?"
            },
            {
                "direction": "inbound",
                "content": broker_response
            }
        ],
        expected={
            "updates": [],
            "events": [expected_event],
            "row_complete": False
        },
        forbidden={
            "updates": ["Leasing Contact", "Email"],
            "requests": []
        },
        metadata={"new_property": new_prop, "diff_contact": diff_contact}
    )


# ============================================================================
# MAIN GENERATOR
# ============================================================================

def generate_full_suite(
    output_dir: str,
    properties_per_category: int = 20,
    templates_per_type: int = 3
) -> Dict:
    """Generate the complete test suite."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate properties
    properties = [generate_property(i) for i in range(properties_per_category * 5)]

    all_tests = []
    manifest = {
        "generated_at": datetime.now().isoformat(),
        "properties_count": len(properties),
        "categories": {}
    }

    # Response Types
    response_type_tests = []

    # Complete info tests
    for i in range(properties_per_category):
        for _ in range(templates_per_type):
            test = generate_complete_info_test(properties[i], len(response_type_tests))
            response_type_tests.append(test)

    # Partial info tests
    for i in range(properties_per_category):
        test = generate_partial_info_test(properties[properties_per_category + i], len(response_type_tests))
        response_type_tests.append(test)

    # Unavailable tests
    for i in range(properties_per_category):
        test = generate_unavailable_test(properties[properties_per_category * 2 + i], len(response_type_tests))
        response_type_tests.append(test)

    # New property tests
    for i in range(properties_per_category // 2):
        test = generate_new_property_test(properties[i], False, i)
        response_type_tests.append(test)
        test = generate_new_property_test(properties[i + properties_per_category // 2], True, i)
        response_type_tests.append(test)

    manifest["categories"]["response_types"] = len(response_type_tests)
    all_tests.extend(response_type_tests)

    # Save response types
    response_dir = output_path / "response_types"
    response_dir.mkdir(exist_ok=True)
    for test in response_type_tests:
        with open(response_dir / f"{test.id}.json", "w") as f:
            json.dump(asdict(test), f, indent=2)

    # Escalation Tests
    escalation_tests = []
    escalation_types = list(ESCALATION_TEMPLATES.keys())

    for esc_type in escalation_types:
        for i in range(properties_per_category):
            prop_idx = (escalation_types.index(esc_type) * properties_per_category + i) % len(properties)
            test = generate_escalation_test(properties[prop_idx], esc_type, i)
            escalation_tests.append(test)

    manifest["categories"]["escalations"] = len(escalation_tests)
    all_tests.extend(escalation_tests)

    # Save escalations
    esc_dir = output_path / "escalations"
    esc_dir.mkdir(exist_ok=True)
    for test in escalation_tests:
        with open(esc_dir / f"{test.id}.json", "w") as f:
            json.dump(asdict(test), f, indent=2)

    # Edge Case Tests
    edge_tests = []
    edge_types = list(EDGE_CASE_TEMPLATES.keys())

    for edge_type in edge_types:
        for i in range(min(10, properties_per_category)):
            prop_idx = (edge_types.index(edge_type) * 10 + i) % len(properties)
            test = generate_edge_case_test(properties[prop_idx], edge_type, i)
            edge_tests.append(test)

    manifest["categories"]["edge_cases"] = len(edge_tests)
    all_tests.extend(edge_tests)

    # Save edge cases
    edge_dir = output_path / "edge_cases"
    edge_dir.mkdir(exist_ok=True)
    for test in edge_tests:
        with open(edge_dir / f"{test.id}.json", "w") as f:
            json.dump(asdict(test), f, indent=2)

    # Save manifest
    manifest["total_tests"] = len(all_tests)
    manifest["test_ids"] = [t.id for t in all_tests]

    with open(output_path / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    # Save properties
    with open(output_path / "properties.json", "w") as f:
        json.dump(properties, f, indent=2)

    return manifest


def main():
    parser = argparse.ArgumentParser(description="Generate test suite")
    parser.add_argument("--output", "-o", default="tests/generated_suite", help="Output directory")
    parser.add_argument("--properties", "-p", type=int, default=20, help="Properties per category")
    parser.add_argument("--templates", "-t", type=int, default=3, help="Templates per type")
    parser.add_argument("--list-categories", action="store_true", help="List categories")
    args = parser.parse_args()

    if args.list_categories:
        print("\nTest Categories:")
        print("  response_types: complete_info, partial_info, unavailable, new_property")
        print("  escalations: identity, budget, size, negotiation, tour, call, contract")
        print("  edge_cases: hostile, out_of_office, forward, wrong_person, short, mixed, issue")
        return

    print(f"\nGenerating test suite...")
    print(f"  Output: {args.output}")
    print(f"  Properties per category: {args.properties}")
    print(f"  Templates per type: {args.templates}")

    manifest = generate_full_suite(
        args.output,
        properties_per_category=args.properties,
        templates_per_type=args.templates
    )

    print(f"\nGenerated {manifest['total_tests']} test cases:")
    for cat, count in manifest["categories"].items():
        print(f"  {cat}: {count}")

    print(f"\nTest suite saved to: {args.output}")


if __name__ == "__main__":
    main()
