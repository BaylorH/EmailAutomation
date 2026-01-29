"""
Multi-Turn Live Email Test Scenarios

Defines broker reply scripts and expected outcomes for each turn of
multi-turn email conversations that run through the real pipeline.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum


class TurnAction(Enum):
    SEND_OUTREACH = "send_outreach"
    BROKER_REPLY = "broker_reply"
    USER_INPUT = "user_input"


class PropertyStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NEEDS_ACTION = "needs_action"
    NON_VIABLE = "non_viable"


@dataclass
class ExpectedNotification:
    kind: str  # sheet_update, action_needed, row_completed, property_unavailable
    meta_contains: Dict[str, str] = field(default_factory=dict)


@dataclass
class TurnSpec:
    """Specification for a single turn in a multi-turn conversation."""
    action: TurnAction
    body: str  # Email body to send (broker reply or user input)
    description: str  # Human-readable description of what this turn does

    # Expected outcomes after pipeline runs
    expected_sheet_values: Dict[str, str] = field(default_factory=dict)
    expected_notification_kinds: List[str] = field(default_factory=list)
    expected_response_type: Optional[str] = None  # closing, missing_fields, forward_to_user, None
    expected_status: PropertyStatus = PropertyStatus.IN_PROGRESS
    expect_auto_reply: bool = True  # Should the AI send an auto-reply?
    expected_thread_message_count: Optional[int] = None  # Total messages in thread after this turn
    # Expected escalation details (for action_needed notifications)
    expected_escalation_reason: Optional[str] = None  # e.g. "needs_user_input:confidential"


@dataclass
class MultiTurnScenario:
    """A complete multi-turn test scenario."""
    name: str
    description: str
    property_address: str
    city: str
    contact_name: str
    contact_email: str  # The "broker" email (Gmail)
    outreach_subject: str
    outreach_body: str
    turns: List[TurnSpec]

    # Final expected state
    final_sheet_values: Dict[str, str] = field(default_factory=dict)
    final_status: PropertyStatus = PropertyStatus.COMPLETE
    # Contextual keywords expected in Listing Brokers Comments
    expected_comments_contain: List[str] = field(default_factory=list)
    # Values that should NOT appear in comments (redundant with columns)
    forbidden_in_comments: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenario 1: Gradual Info Gathering
# 4 turns after outreach: partial → more → remaining → closing
# ---------------------------------------------------------------------------

GRADUAL_INFO_GATHERING = MultiTurnScenario(
    name="gradual_info_gathering",
    description="Broker provides info across 3 replies, AI gathers until complete, sends closing",
    property_address="9250 Baymeadows Rd",
    city="Jacksonville",
    contact_name="Mike Torres",
    contact_email="bp21harrison@gmail.com",
    outreach_subject="Inquiry - 9250 Baymeadows Rd, Jacksonville",
    outreach_body=(
        "Hi Mike,\n\n"
        "I'm reaching out on behalf of a client looking for industrial space "
        "in the Jacksonville area. Could you provide details on the property "
        "at 9250 Baymeadows Rd?\n\n"
        "Specifically, we'd like to know:\n"
        "- Total square footage\n"
        "- Operating expenses per SF\n"
        "- Number of drive-in doors\n"
        "- Number of dock doors\n"
        "- Ceiling height\n"
        "- Power availability\n\n"
        "Thank you,\n"
        "Jill"
    ),
    turns=[
        # Turn 1: Broker provides partial info (SF + rent only) with contextual NNN detail
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides SF and rent only",
            body=(
                "Hi Jill,\n\n"
                "Thanks for reaching out. Here are some details on 9250 Baymeadows:\n\n"
                "The building is 22,500 SF total. It was renovated in 2021 and "
                "is available for immediate occupancy.\n"
                "We're asking $7.25/SF NNN. The space is fully sprinklered "
                "and the yard is fenced with 8 trailer parking spots.\n\n"
                "Let me know if you need anything else.\n\n"
                "Best,\n"
                "Mike Torres"
            ),
            expected_sheet_values={
                "Total SF": "22500",
            },
            expected_notification_kinds=["sheet_update"],
            expected_response_type="missing_fields",
            expect_auto_reply=True,
            expected_status=PropertyStatus.IN_PROGRESS,
            expected_thread_message_count=3,  # outreach + broker reply + AI follow-up
        ),
        # Turn 2: Broker provides docks, ceiling, drive-ins
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides docks, ceiling height, drive-ins",
            body=(
                "Sure thing, Jill.\n\n"
                "The space has 3 dock-high doors and 2 drive-in doors.\n"
                "Ceiling height is 24' clear.\n"
                "It's located right off I-95, easy access to the port.\n\n"
                "Thanks,\n"
                "Mike"
            ),
            expected_sheet_values={
                "Total SF": "22500",
                "Docks": "3",
                "Drive Ins": "2",
                "Ceiling Ht": "24",
            },
            expected_notification_kinds=["sheet_update"],
            expected_response_type="missing_fields",
            expect_auto_reply=True,
            expected_status=PropertyStatus.IN_PROGRESS,
            expected_thread_message_count=5,  # +broker reply + AI follow-up
        ),
        # Turn 3: Broker provides remaining info (power, ops ex) with lease term context
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides power and operating expenses - should complete",
            body=(
                "Jill,\n\n"
                "Power is 800 amps, 3-phase, 480V.\n"
                "Operating expenses are $2.15/SF.\n\n"
                "The owner is flexible on lease terms, 3-7 years. "
                "TI allowance is negotiable depending on term length.\n\n"
                "Anything else you need?\n\n"
                "Mike"
            ),
            expected_sheet_values={
                "Total SF": "22500",
                "Docks": "3",
                "Drive Ins": "2",
                "Ceiling Ht": "24",
                "Power": "800 amps, 3-phase, 480V",
                "Ops Ex /SF": "2.15",
            },
            expected_notification_kinds=["sheet_update"],
            expected_response_type="closing",
            expect_auto_reply=True,
            expected_status=PropertyStatus.COMPLETE,
            expected_thread_message_count=7,  # +broker reply + AI closing
        ),
    ],
    final_sheet_values={
        "Total SF": "22500",
        "Docks": "3",
        "Drive Ins": "2",
        "Ceiling Ht": "24",
        "Power": "800 amps, 3-phase, 480V",
        "Ops Ex /SF": "2.15",
    },
    final_status=PropertyStatus.COMPLETE,
    expected_comments_contain=["nnn", "sprinkler", "fenced"],
    forbidden_in_comments=["22500", "22,500", "2.15", "7.25", "800"],
)


# ---------------------------------------------------------------------------
# Scenario 2: Escalation and Resume
# Broker asks identity question → AI escalates → user provides input → broker
# gives all info → closing
# ---------------------------------------------------------------------------

ESCALATION_AND_RESUME = MultiTurnScenario(
    name="escalation_and_resume",
    description="Broker asks who the client is, AI escalates, user responds, broker provides all info",
    property_address="4710 Southside Blvd",
    city="Jacksonville",
    contact_name="Sarah Chen",
    contact_email="bp21harrison@gmail.com",
    outreach_subject="Inquiry - 4710 Southside Blvd, Jacksonville",
    outreach_body=(
        "Hi Sarah,\n\n"
        "I have a client interested in industrial space in Jacksonville. "
        "Could you share details on the availability at 4710 Southside Blvd?\n\n"
        "We're looking for information on square footage, ceiling height, "
        "dock doors, drive-in doors, power, and operating expenses.\n\n"
        "Thanks,\n"
        "Jill"
    ),
    turns=[
        # Turn 1: Broker asks who the client is → escalation
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker asks identity question - AI should escalate",
            body=(
                "Hi Jill,\n\n"
                "Thanks for reaching out. Before I send over the details, "
                "can you tell me who your client is? We like to know who "
                "we're working with.\n\n"
                "Thanks,\n"
                "Sarah Chen"
            ),
            expected_sheet_values={},
            expected_notification_kinds=["action_needed"],
            expected_response_type="forward_to_user",
            expect_auto_reply=False,  # AI should NOT auto-reply to identity questions
            expected_status=PropertyStatus.NEEDS_ACTION,
            expected_thread_message_count=2,  # outreach + broker reply (no AI reply)
            expected_escalation_reason="needs_user_input:confidential",
        ),
        # Turn 2: User provides input via frontend (outbox entry, like clicking Send in modal)
        TurnSpec(
            action=TurnAction.USER_INPUT,
            description="User (Jill) responds to identity question via frontend modal",
            body=(
                "Hi Sarah,\n\n"
                "I appreciate you asking. My client is a growing logistics company "
                "looking to expand their warehouse operations in the Jacksonville area. "
                "They prefer to remain confidential at this stage, but I can assure you "
                "they are a well-established business.\n\n"
                "Would you be able to share the property details?\n\n"
                "Thanks,\n"
                "Jill"
            ),
            expected_sheet_values={},
            expected_notification_kinds=[],
            expected_response_type=None,  # No AI processing for user input
            expect_auto_reply=False,
            expected_status=PropertyStatus.NEEDS_ACTION,  # Still needs action until broker replies
            expected_thread_message_count=3,  # +user reply
        ),
        # Turn 3: Broker provides all info → closing
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides all info after identity is addressed - should complete",
            body=(
                "Thanks Jill, I understand. No problem at all.\n\n"
                "Here are the details for 4710 Southside Blvd:\n\n"
                "- Total SF: 35,000\n"
                "- Ceiling Height: 28' clear\n"
                "- Dock Doors: 6\n"
                "- Drive-In Doors: 2\n"
                "- Power: 1200 amps, 3-phase\n"
                "- Operating Expenses: $1.95/SF\n"
                "- Rent: $6.50/SF NNN\n\n"
                "The building was constructed in 2019, tilt-up concrete. "
                "It's fully sprinklered with ESFR heads. The space is divisible "
                "down to 15,000 SF if needed. We're flexible on a 3-5 year term.\n\n"
                "Let me know if your client would like to schedule a tour.\n\n"
                "Best,\n"
                "Sarah"
            ),
            expected_sheet_values={
                "Total SF": "35000",
                "Ceiling Ht": "28",
                "Docks": "6",
                "Drive Ins": "2",
                "Power": "1200 amps, 3-phase",
                "Ops Ex /SF": "1.95",
            },
            expected_notification_kinds=["sheet_update"],
            expected_response_type="closing",
            expect_auto_reply=True,
            expected_status=PropertyStatus.COMPLETE,
            expected_thread_message_count=5,  # +broker reply + AI closing
        ),
    ],
    final_sheet_values={
        "Total SF": "35000",
        "Ceiling Ht": "28",
        "Docks": "6",
        "Drive Ins": "2",
        "Power": "1200 amps, 3-phase",
        "Ops Ex /SF": "1.95",
    },
    final_status=PropertyStatus.COMPLETE,
    expected_comments_contain=["nnn", "tilt-up", "esfr", "divisible"],
    forbidden_in_comments=["35000", "35,000", "1.95", "6.50", "1200"],
)


# ---------------------------------------------------------------------------
# Scenario 3: Mixed Info + Question
# Broker provides some info but also asks about budget → AI extracts AND
# escalates → user responds → broker sends rest → closing
# ---------------------------------------------------------------------------

MIXED_INFO_AND_QUESTION = MultiTurnScenario(
    name="mixed_info_and_question",
    description="Broker provides partial info and asks about budget, AI extracts fields AND escalates",
    property_address="7800 Belfort Pkwy",
    city="Jacksonville",
    contact_name="David Park",
    contact_email="bp21harrison@gmail.com",
    outreach_subject="Inquiry - 7800 Belfort Pkwy, Jacksonville",
    outreach_body=(
        "Hi David,\n\n"
        "I'm reaching out regarding available industrial space at "
        "7800 Belfort Pkwy in Jacksonville. My client is looking for "
        "warehouse/distribution space in the area.\n\n"
        "Could you provide the following details:\n"
        "- Total SF\n"
        "- Operating expenses\n"
        "- Dock and drive-in doors\n"
        "- Ceiling height\n"
        "- Power specs\n\n"
        "Thank you,\n"
        "Jill"
    ),
    turns=[
        # Turn 1: Broker provides SF + ceiling but asks about budget
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides some info but asks about budget - AI extracts AND escalates",
            body=(
                "Hi Jill,\n\n"
                "Thanks for your interest in 7800 Belfort Pkwy.\n\n"
                "The building is 18,000 SF with 26' clear ceiling height. "
                "It's a concrete block building built in 2015, fully sprinklered. "
                "Zoned M-1 for light industrial use.\n\n"
                "Before I go further, what's your client's budget range? "
                "We have some flexibility on the rate depending on the "
                "lease term and tenant improvements needed.\n\n"
                "Best regards,\n"
                "David Park"
            ),
            expected_sheet_values={
                "Total SF": "18000",
                "Ceiling Ht": "26",
            },
            expected_notification_kinds=["sheet_update", "action_needed"],
            expected_response_type="forward_to_user",
            expect_auto_reply=False,  # AI escalates budget questions
            expected_status=PropertyStatus.NEEDS_ACTION,
            expected_thread_message_count=2,  # outreach + broker reply (no AI reply)
            expected_escalation_reason="needs_user_input:client_question",
        ),
        # Turn 2: User responds about budget via frontend modal
        TurnSpec(
            action=TurnAction.USER_INPUT,
            description="User (Jill) responds to budget question via frontend modal",
            body=(
                "Hi David,\n\n"
                "My client is looking in the $5-7/SF NNN range, with some "
                "flexibility for the right space. They'd be interested in a "
                "3-5 year term.\n\n"
                "Could you also send over the remaining specs - dock doors, "
                "drive-ins, power, and operating expenses?\n\n"
                "Thanks,\n"
                "Jill"
            ),
            expected_sheet_values={
                "Total SF": "18000",
                "Ceiling Ht": "26",
            },
            expected_notification_kinds=[],
            expected_response_type=None,
            expect_auto_reply=False,
            expected_status=PropertyStatus.NEEDS_ACTION,
            expected_thread_message_count=3,  # +user reply
        ),
        # Turn 3: Broker provides remaining fields → complete
        TurnSpec(
            action=TurnAction.BROKER_REPLY,
            description="Broker provides remaining info - should complete",
            body=(
                "Jill,\n\n"
                "That budget range works. Here are the remaining details:\n\n"
                "- Dock Doors: 4\n"
                "- Drive-In Doors: 1\n"
                "- Power: 600 amps, 3-phase, 277/480V\n"
                "- Operating Expenses: $2.50/SF\n\n"
                "We could do $6.25/SF NNN for a 5-year term. The space is "
                "available immediately, as-is condition. There's a small "
                "fenced yard area on the north side near the Baymeadows exit.\n\n"
                "Let me know if your client wants to see the space.\n\n"
                "Best,\n"
                "David"
            ),
            expected_sheet_values={
                "Total SF": "18000",
                "Ceiling Ht": "26",
                "Docks": "4",
                "Drive Ins": "1",
                "Power": "600 amps, 3-phase, 277/480V",
                "Ops Ex /SF": "2.50",
            },
            expected_notification_kinds=["sheet_update"],
            expected_response_type="closing",
            expect_auto_reply=True,
            expected_status=PropertyStatus.COMPLETE,
            expected_thread_message_count=5,  # +broker reply + AI closing
        ),
    ],
    final_sheet_values={
        "Total SF": "18000",
        "Ceiling Ht": "26",
        "Docks": "4",
        "Drive Ins": "1",
        "Power": "600 amps, 3-phase, 277/480V",
        "Ops Ex /SF": "2.50",
    },
    final_status=PropertyStatus.COMPLETE,
    expected_comments_contain=["nnn", "m-1", "sprinkler", "as-is"],
    forbidden_in_comments=["18000", "18,000", "2.50", "6.25", "600"],
)


# All scenarios
ALL_SCENARIOS = {
    "gradual_info_gathering": GRADUAL_INFO_GATHERING,
    "escalation_and_resume": ESCALATION_AND_RESUME,
    "mixed_info_and_question": MIXED_INFO_AND_QUESTION,
}
