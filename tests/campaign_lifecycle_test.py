#!/usr/bin/env python3
"""
Campaign Lifecycle E2E Test Suite
==================================
Tests the FULL campaign lifecycle from start to finish:
1. Initial outreach to multiple properties
2. Various broker response scenarios (complete, partial, unavailable, etc.)
3. Multi-turn conversations until resolution
4. Sheet state changes (rows filled, moved below NON-VIABLE)
5. Notification flow at each stage
6. Campaign completion detection
7. Threading logic: pause when escalated, resume after user input
8. Timestamp-based processing order validation

This simulates what happens in production when a user launches a campaign
and processes broker responses over time.

Usage:
    python tests/campaign_lifecycle_test.py                  # Run full lifecycle
    python tests/campaign_lifecycle_test.py --scenario X     # Run specific scenario
    python tests/campaign_lifecycle_test.py --list           # List scenarios
"""

import os
import sys
import json
import copy
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum, auto

# ============================================================================
# ENVIRONMENT SETUP
# ============================================================================

env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

if not os.getenv("OPENAI_API_KEY"):
    print("OPENAI_API_KEY environment variable not set")
    sys.exit(1)

for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.getenv(var):
        os.environ[var] = f"test-{var.lower()}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firestore before importing production code
from unittest.mock import MagicMock
import sys as _sys

mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
_sys.modules['google.cloud.firestore'] = mock_firestore
_sys.modules['google.cloud'] = MagicMock()
_sys.modules['google.oauth2.credentials'] = MagicMock()
_sys.modules['google.auth.transport.requests'] = MagicMock()
_sys.modules['googleapiclient.discovery'] = MagicMock()

from email_automation.ai_processing import propose_sheet_updates
from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE

# ============================================================================
# DATA TYPES
# ============================================================================

class PropertyStatus(Enum):
    """Status of a property in the campaign."""
    PENDING = auto()          # No response yet
    IN_PROGRESS = auto()      # Partially filled
    COMPLETE = auto()         # All required fields filled
    NON_VIABLE = auto()       # Moved below divider
    NEEDS_ACTION = auto()     # Waiting for user action
    CLOSED = auto()           # Conversation closed


@dataclass
class PropertyState:
    """Current state of a property in the simulated sheet."""
    address: str
    city: str
    contact: str
    email: str
    row_number: int
    status: PropertyStatus = PropertyStatus.PENDING

    # Current values (simulates sheet row)
    values: Dict[str, str] = field(default_factory=dict)

    # Conversation history
    conversation: List[Dict] = field(default_factory=list)
    turn_count: int = 0

    # Notifications received
    notifications: List[Dict] = field(default_factory=list)

    # Pending action (for user input scenarios)
    pending_action: Optional[Dict] = None

    # Is below NON-VIABLE divider
    is_below_divider: bool = False


@dataclass
class CampaignState:
    """Full state of a simulated campaign."""
    properties: Dict[str, PropertyState] = field(default_factory=dict)
    divider_row: int = 100  # Initial divider position
    total_notifications: List[Dict] = field(default_factory=list)
    campaign_complete: bool = False


# ============================================================================
# BROKER RESPONSE GENERATORS
# ============================================================================

class BrokerResponseGenerator:
    """Generates realistic broker responses for different scenarios."""

    @staticmethod
    def complete_info(prop: PropertyState) -> str:
        """Broker provides all required information."""
        return f"""Hi,

Happy to help with {prop.address}. Here are the complete details:

- Total SF: 15,000
- Rent: $7.50/SF NNN
- NNN/CAM: $2.25/SF
- Drive-ins: 2
- Dock doors: 4
- Ceiling height: 24'
- Power: 400 amps, 3-phase

Available immediately. Let me know if you have questions.

{prop.contact.split()[0] if prop.contact else 'Best'}"""

    @staticmethod
    def partial_info_turn1(prop: PropertyState) -> str:
        """Broker provides only partial information (first turn)."""
        return f"""Hi,

The space at {prop.address} is 12,000 SF with asking rent of $6.50/SF NNN.

Let me know if you need anything else.

{prop.contact.split()[0] if prop.contact else 'Best'}"""

    @staticmethod
    def partial_info_turn2(prop: PropertyState) -> str:
        """Broker provides remaining information (second turn)."""
        return f"""Hi,

Sure, here are the additional details:

- NNN/CAM: $1.85/SF
- 2 dock doors, 1 drive-in
- Clear height: 22'
- Power: 200 amps

Thanks,
{prop.contact.split()[0] if prop.contact else ''}"""

    @staticmethod
    def property_unavailable(prop: PropertyState) -> str:
        """Broker says property is no longer available."""
        return f"""Hi,

Unfortunately {prop.address} is no longer available - we just signed a lease last week.

If anything else comes up in the area I'll let you know.

{prop.contact.split()[0] if prop.contact else 'Thanks'}"""

    @staticmethod
    def unavailable_with_alternative(prop: PropertyState) -> str:
        """Property unavailable but broker suggests alternative."""
        return f"""Hi,

Sorry, {prop.address} just got leased. However, I have another property that might work:

456 Commerce Blvd in Martinez - similar size around 14,000 SF. Here's the listing: https://example.com/456-commerce

Let me know if you want details.

{prop.contact.split()[0] if prop.contact else 'Best'}"""

    @staticmethod
    def new_property_different_contact(prop: PropertyState) -> str:
        """Broker suggests new property with different contact."""
        return f"""Hey,

I can help with {prop.address}, but you should also reach out to Joe at joe@otherbroker.com about 789 Warehouse Way - it's a great option too.

{prop.contact.split()[0] if prop.contact else 'Best'}"""

    @staticmethod
    def call_requested(prop: PropertyState) -> str:
        """Broker wants to discuss over phone."""
        return f"""Hi,

I'd prefer to discuss {prop.address} over the phone - there are some details that would be easier to explain.

Can you call me at 555-123-4567?

{prop.contact.split()[0] if prop.contact else 'Thanks'}"""

    @staticmethod
    def tour_offered(prop: PropertyState) -> str:
        """Broker offers a tour."""
        return f"""Hi,

{prop.address} is available. Would you like to schedule a tour? I'm free Tuesday at 2pm or Wednesday morning.

{prop.contact.split()[0] if prop.contact else 'Let me know'}"""

    @staticmethod
    def identity_question(prop: PropertyState) -> str:
        """Broker asks about client identity."""
        return f"""Hi,

Before I send the details on {prop.address}, can you tell me who your client is? What company are they with?

{prop.contact.split()[0] if prop.contact else 'Thanks'}"""

    @staticmethod
    def budget_question(prop: PropertyState) -> str:
        """Broker asks about budget."""
        return f"""Hi,

The property at {prop.address} is 18,000 SF with 24' clear.

What's the budget range your client is working with? That'll help me know if this is a good fit.

{prop.contact.split()[0] if prop.contact else 'Thanks'}"""

    @staticmethod
    def negotiation_attempt(prop: PropertyState) -> str:
        """Broker makes a counteroffer."""
        return f"""Hi,

Regarding {prop.address} - the landlord is firm at $8.50/SF, but if your client can commit to a 5-year term instead of 3, they could potentially do $7.75/SF. Would they consider that?

{prop.contact.split()[0] if prop.contact else 'Let me know'}"""

    @staticmethod
    def close_conversation(prop: PropertyState) -> str:
        """Natural conversation end."""
        return f"""You're welcome! Let me know if you need anything else. Good luck with the search!

{prop.contact.split()[0] if prop.contact else 'Best'}"""


# ============================================================================
# CAMPAIGN SIMULATION ENGINE
# ============================================================================

class CampaignSimulator:
    """Simulates a full campaign lifecycle."""

    REQUIRED_FIELDS = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]

    HEADER = [
        "Property Address", "City", "Property Name", "Leasing Company",
        "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
        "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
        "Listing Brokers Comments", "Flyer / Link", "Floorplan",
        "Jill and Clients comments"
    ]

    def __init__(self):
        self.state = CampaignState()
        self.results = []

    def add_property(self, address: str, city: str, contact: str, email: str, row: int):
        """Add a property to the campaign."""
        prop = PropertyState(
            address=address,
            city=city,
            contact=contact,
            email=email,
            row_number=row,
            values={
                "property address": address,
                "city": city,
                "leasing contact": contact,
                "email": email
            }
        )
        self.state.properties[address] = prop

    def build_rowvals(self, prop: PropertyState) -> List[str]:
        """Build row values array from property state."""
        rowvals = []
        for col in self.HEADER:
            key = col.lower().strip()
            rowvals.append(prop.values.get(key, ""))
        return rowvals

    def build_conversation_payload(self, prop: PropertyState) -> List[Dict]:
        """Build conversation payload for AI processing."""
        payload = []
        for i, msg in enumerate(prop.conversation):
            payload.append({
                "direction": msg["direction"],
                "from": prop.email if msg["direction"] == "inbound" else "jill@company.com",
                "to": ["jill@company.com"] if msg["direction"] == "inbound" else [prop.email],
                "subject": f"{prop.address}, {prop.city}",
                "timestamp": f"2024-01-15T{10+i}:00:00Z",
                "preview": msg["content"][:200],
                "content": msg["content"]
            })
        return payload

    def process_broker_response(self, address: str, broker_response: str) -> Dict:
        """
        Process a broker response through the AI.
        Returns the AI proposal.
        """
        prop = self.state.properties[address]

        # Add broker response to conversation
        prop.conversation.append({
            "direction": "inbound",
            "content": broker_response
        })
        prop.turn_count += 1

        # Call production AI
        proposal = propose_sheet_updates(
            uid="campaign-test-user",
            client_id="campaign-test-client",
            email=prop.email,
            sheet_id="campaign-test-sheet",
            header=self.HEADER,
            rownum=prop.row_number,
            rowvals=self.build_rowvals(prop),
            thread_id=f"thread-{address.lower().replace(' ', '-')}",
            contact_name=prop.contact,
            conversation=self.build_conversation_payload(prop),
            dry_run=True
        )

        # Apply updates to property state
        if proposal:
            # Apply field updates
            for update in proposal.get("updates", []):
                col = update.get("column", "").lower().strip()
                val = update.get("value", "")
                prop.values[col] = val

                # Create notification
                notif = {"kind": "sheet_update", "column": update.get("column"), "value": val}
                prop.notifications.append(notif)
                self.state.total_notifications.append(notif)

            # Process events
            for event in proposal.get("events", []):
                event_type = event.get("type", "")

                if event_type == "property_unavailable":
                    prop.status = PropertyStatus.NON_VIABLE
                    prop.is_below_divider = True
                    notif = {"kind": "property_unavailable", "address": address}
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)

                elif event_type == "new_property":
                    notif = {
                        "kind": "action_needed",
                        "reason": "new_property_pending_approval",
                        "address": event.get("address"),
                        "email": event.get("email"),
                        "contactName": event.get("contactName")
                    }
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)
                    prop.pending_action = notif

                elif event_type == "call_requested":
                    prop.status = PropertyStatus.NEEDS_ACTION
                    notif = {"kind": "action_needed", "reason": "call_requested"}
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)
                    prop.pending_action = notif

                elif event_type == "tour_requested":
                    prop.status = PropertyStatus.NEEDS_ACTION
                    notif = {
                        "kind": "action_needed",
                        "reason": "tour_requested",
                        "question": event.get("question", "")
                    }
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)
                    prop.pending_action = notif

                elif event_type == "needs_user_input":
                    prop.status = PropertyStatus.NEEDS_ACTION
                    reason = event.get("reason", "unknown")
                    notif = {
                        "kind": "action_needed",
                        "reason": f"needs_user_input:{reason}",
                        "question": event.get("question", "")
                    }
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)
                    prop.pending_action = notif

                elif event_type == "close_conversation":
                    prop.status = PropertyStatus.CLOSED

            # Add AI response to conversation (if any)
            response_email = proposal.get("response_email")
            if response_email:
                prop.conversation.append({
                    "direction": "outbound",
                    "content": response_email
                })

            # Check if row is complete
            if self.is_row_complete(prop):
                if prop.status != PropertyStatus.NON_VIABLE:
                    prop.status = PropertyStatus.COMPLETE
                    notif = {"kind": "row_completed", "address": address}
                    prop.notifications.append(notif)
                    self.state.total_notifications.append(notif)
            elif prop.status == PropertyStatus.PENDING:
                prop.status = PropertyStatus.IN_PROGRESS

        return proposal

    def is_row_complete(self, prop: PropertyState) -> bool:
        """Check if all required fields are filled."""
        for field in self.REQUIRED_FIELDS:
            if not prop.values.get(field, "").strip():
                return False
        return True

    def check_campaign_complete(self) -> bool:
        """Check if the campaign is complete (all properties resolved)."""
        for prop in self.state.properties.values():
            # Campaign is NOT complete if any property is still pending, in progress, or needs user action
            if prop.status in [PropertyStatus.PENDING, PropertyStatus.IN_PROGRESS, PropertyStatus.NEEDS_ACTION]:
                return False
        self.state.campaign_complete = True
        return True

    def get_campaign_summary(self) -> Dict:
        """Get summary of campaign state."""
        summary = {
            "total_properties": len(self.state.properties),
            "complete": 0,
            "non_viable": 0,
            "needs_action": 0,
            "in_progress": 0,
            "pending": 0,
            "closed": 0,
            "total_notifications": len(self.state.total_notifications),
            "campaign_complete": self.state.campaign_complete
        }

        for prop in self.state.properties.values():
            if prop.status == PropertyStatus.COMPLETE:
                summary["complete"] += 1
            elif prop.status == PropertyStatus.NON_VIABLE:
                summary["non_viable"] += 1
            elif prop.status == PropertyStatus.NEEDS_ACTION:
                summary["needs_action"] += 1
            elif prop.status == PropertyStatus.IN_PROGRESS:
                summary["in_progress"] += 1
            elif prop.status == PropertyStatus.PENDING:
                summary["pending"] += 1
            elif prop.status == PropertyStatus.CLOSED:
                summary["closed"] += 1

        return summary

    def simulate_user_response(self, address: str, user_message: str) -> Dict:
        """
        Simulate a user providing input to resume a paused conversation.
        This mimics what happens when:
        1. Property is in NEEDS_ACTION state (conversation paused)
        2. User provides the requested information via the modal
        3. System sends an email and resumes processing

        Returns the AI proposal after the next broker reply.
        """
        prop = self.state.properties[address]

        # Verify property is in a paused state
        if prop.status != PropertyStatus.NEEDS_ACTION:
            raise ValueError(f"Property {address} is not in NEEDS_ACTION state (current: {prop.status})")

        # Clear the pending action (user has addressed it)
        prop.pending_action = None

        # Add user's response to conversation
        prop.conversation.append({
            "direction": "outbound",
            "content": user_message
        })

        # Property is now back in progress (waiting for broker reply)
        prop.status = PropertyStatus.IN_PROGRESS

        return {"status": "resumed", "awaiting_broker_reply": True}

    def is_property_paused(self, address: str) -> bool:
        """Check if a property conversation is paused (needs user action)."""
        prop = self.state.properties.get(address)
        if not prop:
            return False
        return prop.status == PropertyStatus.NEEDS_ACTION

    def get_paused_properties(self) -> List[str]:
        """Get list of all properties currently in paused state."""
        return [addr for addr, prop in self.state.properties.items()
                if prop.status == PropertyStatus.NEEDS_ACTION]

    def get_active_properties(self) -> List[str]:
        """Get list of properties with active (non-paused, non-complete) conversations."""
        return [addr for addr, prop in self.state.properties.items()
                if prop.status in [PropertyStatus.PENDING, PropertyStatus.IN_PROGRESS]]

    def get_resolved_properties(self) -> List[str]:
        """Get list of fully resolved properties (complete, non-viable, or closed)."""
        return [addr for addr, prop in self.state.properties.items()
                if prop.status in [PropertyStatus.COMPLETE, PropertyStatus.NON_VIABLE, PropertyStatus.CLOSED]]


# ============================================================================
# TEST SCENARIOS
# ============================================================================

@dataclass
class CampaignScenario:
    """Defines a complete campaign test scenario."""
    name: str
    description: str
    properties: List[Dict]  # {address, city, contact, email, responses: [...]}
    expected_final_state: Dict  # Expected summary values


CAMPAIGN_SCENARIOS = [
    CampaignScenario(
        name="mixed_outcomes",
        description="5 properties with mixed outcomes: 2 complete, 1 unavailable, 1 needs input, 1 multi-turn",
        properties=[
            {
                "address": "100 Industrial Way",
                "city": "Augusta",
                "contact": "John Smith",
                "email": "john@broker1.com",
                "response_type": "complete_info"
            },
            {
                "address": "200 Commerce Blvd",
                "city": "Evans",
                "contact": "Sarah Jones",
                "email": "sarah@broker2.com",
                "response_type": "property_unavailable"
            },
            {
                "address": "300 Warehouse Dr",
                "city": "Martinez",
                "contact": "Mike Wilson",
                "email": "mike@broker3.com",
                "response_type": "identity_question"
            },
            {
                "address": "400 Distribution Ct",
                "city": "Augusta",
                "contact": "Lisa Brown",
                "email": "lisa@broker4.com",
                "response_type": "partial_multi_turn"  # Will need 2 turns
            },
            {
                "address": "500 Logistics Pkwy",
                "city": "Evans",
                "contact": "Tom Davis",
                "email": "tom@broker5.com",
                "response_type": "complete_info"
            }
        ],
        expected_final_state={
            "complete": 3,  # 100 Industrial, 400 Distribution (after 2 turns), 500 Logistics
            "non_viable": 1,  # 200 Commerce
            "needs_action": 1,  # 300 Warehouse (identity question)
        }
    ),

    CampaignScenario(
        name="all_complete",
        description="3 properties all provide complete info immediately",
        properties=[
            {
                "address": "101 Perfect St",
                "city": "Augusta",
                "contact": "Alice",
                "email": "alice@broker.com",
                "response_type": "complete_info"
            },
            {
                "address": "102 Perfect St",
                "city": "Evans",
                "contact": "Bob",
                "email": "bob@broker.com",
                "response_type": "complete_info"
            },
            {
                "address": "103 Perfect St",
                "city": "Martinez",
                "contact": "Carol",
                "email": "carol@broker.com",
                "response_type": "complete_info"
            }
        ],
        expected_final_state={
            "complete": 3,
            "non_viable": 0,
            "needs_action": 0,
            "campaign_complete": True
        }
    ),

    CampaignScenario(
        name="all_unavailable",
        description="3 properties all unavailable - campaign ends with all non-viable",
        properties=[
            {
                "address": "201 Gone St",
                "city": "Augusta",
                "contact": "Dave",
                "email": "dave@broker.com",
                "response_type": "property_unavailable"
            },
            {
                "address": "202 Gone St",
                "city": "Evans",
                "contact": "Eve",
                "email": "eve@broker.com",
                "response_type": "property_unavailable"
            },
            {
                "address": "203 Gone St",
                "city": "Martinez",
                "contact": "Frank",
                "email": "frank@broker.com",
                "response_type": "property_unavailable"
            }
        ],
        expected_final_state={
            "complete": 0,
            "non_viable": 3,
            "needs_action": 0,
            "campaign_complete": True
        }
    ),

    CampaignScenario(
        name="new_properties_suggested",
        description="Brokers suggest alternative properties requiring user approval",
        properties=[
            {
                "address": "301 Original St",
                "city": "Augusta",
                "contact": "Grace",
                "email": "grace@broker.com",
                "response_type": "unavailable_with_alternative"
            },
            {
                "address": "302 Original St",
                "city": "Evans",
                "contact": "Henry",
                "email": "henry@broker.com",
                "response_type": "new_property_different_contact"
            }
        ],
        expected_final_state={
            "non_viable": 1,  # 301 is unavailable
            "needs_action": 0,  # New property notifications created but don't change status
            "new_property_notifications": 2
        }
    ),

    CampaignScenario(
        name="escalation_scenarios",
        description="Various scenarios requiring user input",
        properties=[
            {
                "address": "401 Question St",
                "city": "Augusta",
                "contact": "Ivy",
                "email": "ivy@broker.com",
                "response_type": "identity_question"
            },
            {
                "address": "402 Question St",
                "city": "Evans",
                "contact": "Jack",
                "email": "jack@broker.com",
                "response_type": "budget_question"
            },
            {
                "address": "403 Question St",
                "city": "Martinez",
                "contact": "Kate",
                "email": "kate@broker.com",
                "response_type": "negotiation_attempt"
            },
            {
                "address": "404 Question St",
                "city": "Augusta",
                "contact": "Leo",
                "email": "leo@broker.com",
                "response_type": "tour_offered"
            },
            {
                "address": "405 Question St",
                "city": "Evans",
                "contact": "Mia",
                "email": "mia@broker.com",
                "response_type": "call_requested"
            }
        ],
        expected_final_state={
            "needs_action": 5,  # All require user action
            "complete": 0,
            "non_viable": 0
        }
    ),

    CampaignScenario(
        name="multi_turn_completion",
        description="Properties requiring multiple conversation turns to complete",
        properties=[
            {
                "address": "501 Partial St",
                "city": "Augusta",
                "contact": "Nate",
                "email": "nate@broker.com",
                "response_type": "partial_multi_turn"
            },
            {
                "address": "502 Partial St",
                "city": "Evans",
                "contact": "Olivia",
                "email": "olivia@broker.com",
                "response_type": "partial_multi_turn"
            }
        ],
        expected_final_state={
            "complete": 2,  # Both complete after 2 turns
            "non_viable": 0,
            "needs_action": 0,
            "campaign_complete": True
        }
    ),

    # =========================================================================
    # THREADING LOGIC TEST SCENARIOS
    # Tests pause/resume/complete conversation flow
    # =========================================================================

    CampaignScenario(
        name="pause_on_escalation",
        description="Conversations correctly pause when escalated (needs user action)",
        properties=[
            {
                "address": "601 Pause St",
                "city": "Augusta",
                "contact": "Paul",
                "email": "paul@broker.com",
                "response_type": "identity_question",  # Triggers needs_user_input
                "threading_test": "verify_paused"  # Special marker for threading test
            },
            {
                "address": "602 Pause St",
                "city": "Evans",
                "contact": "Quinn",
                "email": "quinn@broker.com",
                "response_type": "tour_offered",  # Triggers tour_requested
                "threading_test": "verify_paused"
            },
            {
                "address": "603 Pause St",
                "city": "Martinez",
                "contact": "Rita",
                "email": "rita@broker.com",
                "response_type": "call_requested",  # Triggers call_requested
                "threading_test": "verify_paused"
            }
        ],
        expected_final_state={
            "needs_action": 3,  # All three should be paused
            "complete": 0,
            "in_progress": 0,
            "paused_count": 3  # Custom check for threading test
        }
    ),

    CampaignScenario(
        name="resume_after_user_input",
        description="Conversations resume after user provides requested input",
        properties=[
            {
                "address": "701 Resume St",
                "city": "Augusta",
                "contact": "Steve",
                "email": "steve@broker.com",
                "response_type": "identity_question",
                "threading_test": "pause_then_resume",
                "user_response": "This inquiry is for a confidential client in the logistics industry. They prefer to remain anonymous until we identify suitable properties.",
                "follow_up_response": "complete_info"  # After resume, broker provides complete info
            }
        ],
        expected_final_state={
            "complete": 1,  # Should complete after resume + broker reply
            "needs_action": 0,
            "campaign_complete": True
        }
    ),

    CampaignScenario(
        name="pause_resume_complete_cycle",
        description="Full cycle: pause → user input → resume → complete for multiple properties",
        properties=[
            {
                "address": "801 Cycle St",
                "city": "Augusta",
                "contact": "Tara",
                "email": "tara@broker.com",
                "response_type": "tour_offered",
                "threading_test": "full_cycle",
                "user_response": "Yes, we would like to schedule a tour. Please let us know available times next week.",
                "follow_up_response": "complete_info"
            },
            {
                "address": "802 Cycle St",
                "city": "Evans",
                "contact": "Uma",
                "email": "uma@broker.com",
                "response_type": "budget_question",
                "threading_test": "full_cycle",
                "user_response": "The client's budget is flexible within market range. What rates are you seeing for this type of space?",
                "follow_up_response": "complete_info"
            }
        ],
        expected_final_state={
            "complete": 2,  # Both complete after full cycle
            "needs_action": 0,
            "campaign_complete": True,
            "total_turns_min": 4  # At least 2 turns per property
        }
    ),

    CampaignScenario(
        name="mixed_pause_and_complete",
        description="Some properties complete immediately, others pause - campaign not complete until all resolved",
        properties=[
            {
                "address": "901 Mix St",
                "city": "Augusta",
                "contact": "Victor",
                "email": "victor@broker.com",
                "response_type": "complete_info"  # Completes immediately
            },
            {
                "address": "902 Mix St",
                "city": "Evans",
                "contact": "Wendy",
                "email": "wendy@broker.com",
                "response_type": "identity_question",  # Pauses
                "threading_test": "verify_blocks_campaign"
            },
            {
                "address": "903 Mix St",
                "city": "Martinez",
                "contact": "Xavier",
                "email": "xavier@broker.com",
                "response_type": "complete_info"  # Completes immediately
            }
        ],
        expected_final_state={
            "complete": 2,
            "needs_action": 1,  # One still paused
            "campaign_complete": False  # Campaign NOT complete because one is paused
        }
    ),

    CampaignScenario(
        name="close_conversation_terminates",
        description="Close conversation event properly terminates without needing user action",
        properties=[
            {
                "address": "1001 Close St",
                "city": "Augusta",
                "contact": "Yara",
                "email": "yara@broker.com",
                "response_type": "close_conversation",
                "threading_test": "verify_closed"
            }
        ],
        expected_final_state={
            "closed": 1,
            "needs_action": 0,  # Should NOT need action - just closed
            "campaign_complete": True
        }
    )
]


# ============================================================================
# TEST EXECUTION
# ============================================================================

def run_campaign_scenario(scenario: CampaignScenario, verbose: bool = True) -> Tuple[bool, Dict]:
    """
    Run a complete campaign scenario.
    Returns (passed, results_dict)
    """
    if verbose:
        print(f"\n{'='*70}")
        print(f"CAMPAIGN SCENARIO: {scenario.name}")
        print(f"{'='*70}")
        print(f"Description: {scenario.description}")
        print(f"Properties: {len(scenario.properties)}")

    sim = CampaignSimulator()

    # Add all properties
    for i, prop_def in enumerate(scenario.properties):
        sim.add_property(
            address=prop_def["address"],
            city=prop_def["city"],
            contact=prop_def["contact"],
            email=prop_def["email"],
            row=i + 3  # Start at row 3 (after header in row 2)
        )

    # Process each property's responses
    for prop_def in scenario.properties:
        address = prop_def["address"]
        response_type = prop_def["response_type"]
        prop = sim.state.properties[address]

        if verbose:
            print(f"\n{'─'*60}")
            print(f"Processing: {address}")
            print(f"Response type: {response_type}")

        # Generate initial outbound email (simulated)
        prop.conversation.append({
            "direction": "outbound",
            "content": f"Hi {prop.contact.split()[0] if prop.contact else ''}, I'm interested in {address}. Could you provide availability and details?"
        })

        # Generate and process broker response(s)
        if response_type == "complete_info":
            response = BrokerResponseGenerator.complete_info(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "property_unavailable":
            response = BrokerResponseGenerator.property_unavailable(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "unavailable_with_alternative":
            response = BrokerResponseGenerator.unavailable_with_alternative(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "new_property_different_contact":
            response = BrokerResponseGenerator.new_property_different_contact(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "identity_question":
            response = BrokerResponseGenerator.identity_question(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "budget_question":
            response = BrokerResponseGenerator.budget_question(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "negotiation_attempt":
            response = BrokerResponseGenerator.negotiation_attempt(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "tour_offered":
            response = BrokerResponseGenerator.tour_offered(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "call_requested":
            response = BrokerResponseGenerator.call_requested(prop)
            proposal = sim.process_broker_response(address, response)

        elif response_type == "partial_multi_turn":
            # Turn 1: partial info
            response1 = BrokerResponseGenerator.partial_info_turn1(prop)
            proposal1 = sim.process_broker_response(address, response1)

            if verbose:
                print(f"  Turn 1: Extracted {len(proposal1.get('updates', []))} fields")

            # Turn 2: remaining info
            response2 = BrokerResponseGenerator.partial_info_turn2(prop)
            proposal = sim.process_broker_response(address, response2)

            if verbose:
                print(f"  Turn 2: Extracted {len(proposal.get('updates', []))} more fields")

        elif response_type == "close_conversation":
            response = BrokerResponseGenerator.close_conversation(prop)
            proposal = sim.process_broker_response(address, response)

        # Handle threading test scenarios (pause → resume → complete cycles)
        threading_test = prop_def.get("threading_test")

        if threading_test in ["pause_then_resume", "full_cycle"]:
            # Verify property is now paused
            if not sim.is_property_paused(address):
                if verbose:
                    print(f"  ⚠️ Expected property to be paused but status is {prop.status}")
            else:
                if verbose:
                    print(f"  ✓ Property correctly paused (NEEDS_ACTION)")

                # Simulate user providing input
                user_response = prop_def.get("user_response", "Thank you for your patience. Here is the information you requested.")
                sim.simulate_user_response(address, user_response)

                if verbose:
                    print(f"  → User responded, conversation resumed")

                # Now simulate broker's follow-up response
                follow_up_type = prop_def.get("follow_up_response", "complete_info")
                if follow_up_type == "complete_info":
                    follow_up = BrokerResponseGenerator.complete_info(prop)
                elif follow_up_type == "partial_info":
                    follow_up = BrokerResponseGenerator.partial_info_turn1(prop)
                else:
                    follow_up = BrokerResponseGenerator.complete_info(prop)

                proposal = sim.process_broker_response(address, follow_up)

                if verbose:
                    print(f"  → Broker replied with {follow_up_type}, status now: {prop.status.name}")

        elif threading_test == "verify_paused":
            # Just verify the property is paused - don't resume
            if not sim.is_property_paused(address):
                if verbose:
                    print(f"  ❌ THREADING FAIL: Expected paused but got {prop.status}")

        elif threading_test == "verify_blocks_campaign":
            # This property being paused should prevent campaign completion
            pass  # Will be validated in final summary check

        elif threading_test == "verify_closed":
            # Verify property is in CLOSED state (not NEEDS_ACTION)
            if prop.status != PropertyStatus.CLOSED:
                if verbose:
                    print(f"  ❌ THREADING FAIL: Expected CLOSED but got {prop.status}")

        if verbose:
            print(f"  Status: {prop.status.name}")
            print(f"  Fields filled: {sum(1 for f in sim.REQUIRED_FIELDS if prop.values.get(f))}/{len(sim.REQUIRED_FIELDS)}")
            print(f"  Notifications: {len(prop.notifications)}")

    # Check campaign completion
    sim.check_campaign_complete()

    # Get summary
    summary = sim.get_campaign_summary()

    if verbose:
        print(f"\n{'─'*60}")
        print("CAMPAIGN SUMMARY")
        print(f"{'─'*60}")
        print(f"  Complete: {summary['complete']}")
        print(f"  Non-viable: {summary['non_viable']}")
        print(f"  Needs action: {summary['needs_action']}")
        print(f"  In progress: {summary['in_progress']}")
        print(f"  Total notifications: {summary['total_notifications']}")
        print(f"  Campaign complete: {summary['campaign_complete']}")

    # Validate against expected
    issues = []
    expected = scenario.expected_final_state

    for key, expected_val in expected.items():
        if key == "new_property_notifications":
            # Count new property notifications
            actual = sum(1 for n in sim.state.total_notifications
                        if n.get("reason") == "new_property_pending_approval")
            if actual != expected_val:
                issues.append(f"new_property_notifications: expected {expected_val}, got {actual}")

        elif key == "paused_count":
            # Count properties currently in NEEDS_ACTION (paused) state
            actual = len(sim.get_paused_properties())
            if actual != expected_val:
                issues.append(f"paused_count: expected {expected_val}, got {actual}")

        elif key == "total_turns_min":
            # Verify minimum total turns across all properties
            actual_turns = sum(p.turn_count for p in sim.state.properties.values())
            if actual_turns < expected_val:
                issues.append(f"total_turns: expected at least {expected_val}, got {actual_turns}")

        elif key in summary:
            actual = summary[key]
            if actual != expected_val:
                issues.append(f"{key}: expected {expected_val}, got {actual}")

    passed = len(issues) == 0

    if verbose:
        print(f"\n{'✅ PASS' if passed else '❌ FAIL'}")
        for issue in issues:
            print(f"  ❌ {issue}")

    return passed, {
        "scenario": scenario.name,
        "summary": summary,
        "issues": issues,
        "properties": {
            addr: {
                "status": p.status.name,
                "fields_complete": sum(1 for f in sim.REQUIRED_FIELDS if p.values.get(f)),
                "notifications": len(p.notifications),
                "turns": p.turn_count
            }
            for addr, p in sim.state.properties.items()
        }
    }


def run_all_scenarios(verbose: bool = True) -> List[Dict]:
    """Run all campaign scenarios."""
    results = []

    for scenario in CAMPAIGN_SCENARIOS:
        try:
            passed, result = run_campaign_scenario(scenario, verbose)
            result["passed"] = passed
            results.append(result)
        except Exception as e:
            if verbose:
                print(f"\n❌ ERROR in scenario '{scenario.name}': {e}")
            results.append({
                "scenario": scenario.name,
                "passed": False,
                "error": str(e)
            })

    return results


# ============================================================================
# MAIN
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Campaign Lifecycle E2E Tests")
    parser.add_argument("--scenario", "-s", help="Run specific scenario by name")
    parser.add_argument("--list", "-l", action="store_true", help="List available scenarios")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()

    if args.list:
        print("\nAvailable campaign scenarios:")
        print(f"{'─'*60}")
        for s in CAMPAIGN_SCENARIOS:
            print(f"  {s.name}")
            print(f"    {s.description}")
            print(f"    Properties: {len(s.properties)}")
        return

    print("\n" + "="*70)
    print("CAMPAIGN LIFECYCLE E2E TEST SUITE")
    print("="*70)
    print("Tests complete campaign flows from start to finish")

    if args.scenario:
        # Run specific scenario
        scenario = next((s for s in CAMPAIGN_SCENARIOS if s.name == args.scenario), None)
        if not scenario:
            print(f"Unknown scenario: {args.scenario}")
            print("Use --list to see available scenarios")
            sys.exit(1)

        passed, result = run_campaign_scenario(scenario, not args.quiet)
        sys.exit(0 if passed else 1)

    # Run all scenarios
    results = run_all_scenarios(not args.quiet)

    # Summary
    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)

    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    print(f"Scenarios: {total} | Passed: {passed} | Failed: {total - passed}")
    print(f"Pass Rate: {passed/total*100:.1f}%")

    if passed < total:
        print("\nFailed scenarios:")
        for r in results:
            if not r.get("passed"):
                print(f"  ❌ {r['scenario']}")
                for issue in r.get("issues", []):
                    print(f"      - {issue}")
                if r.get("error"):
                    print(f"      - ERROR: {r['error']}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
