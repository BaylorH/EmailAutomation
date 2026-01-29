#!/usr/bin/env python3
"""
E2E Test Server for EmailAutomation Backend

A simple HTTP server that the frontend E2E tests can call to trigger
backend processing. This runs the REAL backend code against Firebase
emulators with only Graph API mocked.

Usage:
    # Set up environment
    export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
    export E2E_TEST_MODE=true
    export OPENAI_API_KEY=sk-...

    # Start server
    python tests/e2e_server.py --port 5002

    # From frontend E2E tests (JavaScript)
    await fetch('http://localhost:5002/process-outbox', {
        method: 'POST',
        body: JSON.stringify({ userId: 'test-user-123' })
    });
"""

import os
import sys
import json
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# CRITICAL: Set emulator environment BEFORE importing any Firebase modules
os.environ['E2E_TEST_MODE'] = 'true'
os.environ['FIRESTORE_EMULATOR_HOST'] = os.environ.get('FIRESTORE_EMULATOR_HOST', '127.0.0.1:8080')

# Load .env file for API keys
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key not in os.environ:  # Don't override existing env vars
                    os.environ[key] = value

# Set dummy values for required env vars if not present
for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.environ.get(var):
        os.environ[var] = f"test-{var.lower()}"

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.e2e_harness import E2EHarness, create_test_thread

# Try to import AI processing for simulate-response endpoint
try:
    from email_automation.ai_processing import propose_sheet_updates
    AI_AVAILABLE = True
    print("✅ AI processing available")
except (ImportError, RuntimeError) as e:
    AI_AVAILABLE = False
    print(f"⚠️ AI processing not available: {e}")

# In-memory state for simulation testing
MOCK_STATE = {"notifications": [], "sheet_data": {}, "threads": {}}

# Response templates for broker simulation
BROKER_RESPONSES = {
    "complete_info": """Hi,

Happy to help with {address}. Here are the complete details:

- Total SF: 15,000
- Rent: $7.50/SF NNN
- NNN/CAM: $2.25/SF
- Drive-ins: 2
- Dock doors: 4
- Ceiling height: 24'
- Power: 400 amps, 3-phase

Available immediately. Let me know if you have questions.

{contact}""",

    "partial_info": """Hi,

The space at {address} is 12,000 SF with asking rent of $6.50/SF NNN.

Let me know if you need anything else.

{contact}""",

    "complete_remaining": """Hi,

Sure, here are the additional details for {address}:

- NNN/CAM: $1.85/SF
- 2 dock doors, 1 drive-in
- Clear height: 22'
- Power: 200 amps

Thanks,
{contact}""",

    "property_unavailable": """Hi,

Unfortunately {address} is no longer available - we just signed a lease last week.

If anything else comes up in the area I'll let you know.

{contact}""",

    "new_property_different_contact": """Hey,

I can help with {address}, but you should also reach out to Joe at joe@otherbroker.com about 789 Warehouse Way - it's a great option too.

{contact}""",

    "call_requested": """Hi,

I'd prefer to discuss {address} over the phone - there are some details that would be easier to explain.

Can you call me at 555-123-4567?

{contact}""",

    "tour_offered": """Hi,

{address} is available. Would you like to schedule a tour? I'm free Tuesday at 2pm or Wednesday morning.

{contact}""",

    "identity_question": """Hi,

Before I send the details on {address}, can you tell me who your client is? What company are they with?

{contact}""",

    "budget_question": """Hi,

The property at {address} is 18,000 SF with 24' clear.

What's the budget range your client is working with? That'll help me know if this is a good fit.

{contact}""",

    "negotiation_attempt": """Hi,

Regarding {address} - the landlord is firm at $8.50/SF, but if your client can commit to a 5-year term instead of 3, they could potentially do $7.75/SF. Would they consider that?

{contact}"""
}

SHEET_HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]


class E2ERequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for E2E test operations."""

    def _set_headers(self, status=200, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _read_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            return json.loads(self.rfile.read(content_length))
        return {}

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self._set_headers(200)

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            self._set_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "emulator": os.environ.get('FIRESTORE_EMULATOR_HOST'),
                "mode": "e2e-test"
            }).encode())

        elif parsed.path == '/sent-emails':
            # Get sent emails from current session
            self._set_headers()
            # This would need session tracking - for now return empty
            self.wfile.write(json.dumps({"emails": []}).encode())

        elif parsed.path == '/api/campaign-state':
            # Get current campaign state for frontend tests
            self._set_headers()
            result = self._get_campaign_state()
            self.wfile.write(json.dumps(result).encode())

        elif parsed.path == '/api/notifications':
            # Get all notifications
            self._set_headers()
            self.wfile.write(json.dumps({
                "notifications": MOCK_STATE.get('notifications', [])
            }).encode())

        elif parsed.path.startswith('/api/property/'):
            # Get specific property state
            property_key = parsed.path.replace('/api/property/', '')
            self._set_headers()
            if property_key in MOCK_STATE.get('sheet_data', {}):
                self.wfile.write(json.dumps({
                    "key": property_key,
                    "row": MOCK_STATE['sheet_data'][property_key],
                    "header": SHEET_HEADER,
                    "conversation": MOCK_STATE.get('threads', {}).get(property_key, [])
                }).encode())
            else:
                self.wfile.write(json.dumps({"error": "Property not found"}).encode())

        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode())

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        body = self._read_body()

        try:
            if parsed.path == '/process-outbox':
                # Process outbox for a user
                user_id = body.get('userId')
                if not user_id:
                    self._set_headers(400)
                    self.wfile.write(json.dumps({"error": "userId required"}).encode())
                    return

                harness = E2EHarness(user_id)
                result = harness.process_outbox()

                self._set_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "result": result,
                    "sentEmails": harness.get_sent_emails()
                }, default=str).encode())

            elif parsed.path == '/inject-reply':
                # Inject a broker reply and process it
                user_id = body.get('userId')
                from_email = body.get('fromEmail')
                reply_body = body.get('body')
                subject = body.get('subject', 'RE: Property Inquiry')
                conversation_id = body.get('conversationId')
                in_reply_to = body.get('inReplyTo')

                if not all([user_id, from_email, reply_body]):
                    self._set_headers(400)
                    self.wfile.write(json.dumps({
                        "error": "userId, fromEmail, and body required"
                    }).encode())
                    return

                harness = E2EHarness(user_id)

                # Inject the reply
                msg = harness.inject_broker_reply(
                    from_email=from_email,
                    body=reply_body,
                    subject=subject,
                    conversation_id=conversation_id,
                    in_reply_to=in_reply_to
                )

                # Process the inbox to handle the reply
                results = harness.process_inbox()

                self._set_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "injectedMessage": msg,
                    "processedCount": len(results) if results else 0
                }, default=str).encode())

            elif parsed.path == '/process-message':
                # Process a single message through AI extraction
                user_id = body.get('userId')
                message = body.get('message')

                if not all([user_id, message]):
                    self._set_headers(400)
                    self.wfile.write(json.dumps({
                        "error": "userId and message required"
                    }).encode())
                    return

                harness = E2EHarness(user_id)
                result = harness.process_single_message(message)

                self._set_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "result": result
                }, default=str).encode())

            elif parsed.path == '/process-user':
                # Full processing cycle (like GitHub Actions)
                user_id = body.get('userId')
                if not user_id:
                    self._set_headers(400)
                    self.wfile.write(json.dumps({"error": "userId required"}).encode())
                    return

                harness = E2EHarness(user_id)
                result = harness.process_user()

                self._set_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "result": result,
                    "sentEmails": harness.get_sent_emails()
                }, default=str).encode())

            elif parsed.path == '/create-thread':
                # Create a test thread for reply matching
                user_id = body.get('userId')
                client_id = body.get('clientId')
                property_address = body.get('propertyAddress')
                broker_email = body.get('brokerEmail')
                internet_message_id = body.get('internetMessageId')
                conversation_id = body.get('conversationId')

                if not all([user_id, client_id, property_address, broker_email]):
                    self._set_headers(400)
                    self.wfile.write(json.dumps({
                        "error": "userId, clientId, propertyAddress, brokerEmail required"
                    }).encode())
                    return

                thread_id = create_test_thread(
                    user_id=user_id,
                    client_id=client_id,
                    property_address=property_address,
                    broker_email=broker_email,
                    internet_message_id=internet_message_id or f"<test-{property_address}@mock.test>",
                    conversation_id=conversation_id or f"conv-test-{property_address}"
                )

                self._set_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "threadId": thread_id
                }).encode())

            elif parsed.path == '/api/simulate-response':
                # Simulate a broker response with AI processing
                # Used by frontend E2E tests for campaign flow testing
                result = self._handle_simulate_response(body)
                self._set_headers()
                self.wfile.write(json.dumps(result, default=str).encode())

            elif parsed.path == '/api/campaign-grade':
                # Grade campaign results using quality_benchmark scoring
                result = self._handle_campaign_grade(body)
                self._set_headers()
                self.wfile.write(json.dumps(result, default=str).encode())

            elif parsed.path == '/api/reset':
                # Reset test state
                global MOCK_STATE
                MOCK_STATE = {"notifications": [], "sheet_data": {}, "threads": {}}
                self._set_headers()
                self.wfile.write(json.dumps({"status": "reset complete"}).encode())

            else:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "Not found"}).encode())

        except Exception as e:
            import traceback
            print(f"❌ Error: {e}")
            traceback.print_exc()
            self._set_headers(500)
            self.wfile.write(json.dumps({
                "error": str(e),
                "traceback": traceback.format_exc()
            }).encode())

    def log_message(self, format, *args):
        """Custom log format."""
        print(f"📨 {self.address_string()} - {format % args}")

    def _get_campaign_state(self):
        """Get current campaign state summary."""
        global MOCK_STATE

        required = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
        header_map = {h.lower().strip(): i for i, h in enumerate(SHEET_HEADER)}

        properties = []
        for key, row in MOCK_STATE.get('sheet_data', {}).items():
            complete_count = sum(1 for f in required if row[header_map.get(f, 0)].strip())
            properties.append({
                "key": key,
                "address": row[0],
                "city": row[1],
                "contact": row[4],
                "email": row[5],
                "fieldsComplete": complete_count,
                "fieldsRequired": len(required),
                "isComplete": complete_count == len(required),
                "row": row
            })

        return {
            "properties": properties,
            "notifications": MOCK_STATE.get('notifications', []),
            "summary": {
                "total": len(properties),
                "complete": sum(1 for p in properties if p["isComplete"]),
                "inProgress": sum(1 for p in properties if 0 < p["fieldsComplete"] < p["fieldsRequired"]),
                "pending": sum(1 for p in properties if p["fieldsComplete"] == 0)
            }
        }

    def _handle_campaign_grade(self, body):
        """
        Handle /api/campaign-grade endpoint.
        Grades a set of property results using the quality_benchmark scoring functions.

        Body: {
            properties: [{
                actualUpdates: [{column, value}],
                expectedUpdates: [{column, value}],
                actualNotes: str,
                expectedNotes: str,
                forbiddenInNotes: [str],
                actualEvents: [str],
                expectedEvents: [str],
                response: str,
                expectedResponseType: str,
                responseShouldMention: [str],
            }]
        }
        """
        try:
            from tests.quality_benchmark import (
                score_field_accuracy,
                score_field_completeness,
                score_notes_quality,
                score_response_quality,
                score_event_accuracy,
            )
        except ImportError:
            return {"error": "quality_benchmark module not available"}

        properties = body.get("properties", [])
        results = []

        for prop in properties:
            actual_updates = prop.get("actualUpdates", [])
            expected_updates = prop.get("expectedUpdates", [])
            actual_notes = prop.get("actualNotes", "")
            expected_notes = prop.get("expectedNotes", "")
            forbidden_in_notes = prop.get("forbiddenInNotes", [])
            actual_events = prop.get("actualEvents", [])
            expected_events = prop.get("expectedEvents", [])
            response = prop.get("response", "")
            expected_response_type = prop.get("expectedResponseType", "")
            response_should_mention = prop.get("responseShouldMention", [])

            field_acc, field_acc_details = score_field_accuracy(actual_updates, expected_updates)
            field_comp, field_comp_details = score_field_completeness(actual_updates, expected_updates)
            notes_qual, notes_details = score_notes_quality(actual_notes, expected_notes, forbidden_in_notes)
            resp_qual, resp_details = score_response_quality(response, expected_response_type, response_should_mention)
            event_acc, event_details = score_event_accuracy(actual_events, expected_events)

            overall = (
                field_acc * 0.3 +
                field_comp * 0.2 +
                notes_qual * 0.2 +
                resp_qual * 0.2 +
                event_acc * 0.1
            )

            results.append({
                "address": prop.get("address", "unknown"),
                "scores": {
                    "fieldAccuracy": round(field_acc, 3),
                    "fieldCompleteness": round(field_comp, 3),
                    "notesQuality": round(notes_qual, 3),
                    "responseQuality": round(resp_qual, 3),
                    "eventAccuracy": round(event_acc, 3),
                    "overall": round(overall, 3),
                },
                "details": {
                    "fieldAccuracy": field_acc_details,
                    "fieldCompleteness": field_comp_details,
                    "notesQuality": notes_details,
                    "responseQuality": resp_details,
                    "eventAccuracy": event_details,
                },
            })

        # Campaign average
        avg_overall = sum(r["scores"]["overall"] for r in results) / max(1, len(results))

        return {
            "success": True,
            "propertyCount": len(results),
            "averageOverall": round(avg_overall, 3),
            "properties": results,
        }

    def _handle_simulate_response(self, body):
        """
        Handle /api/simulate-response endpoint.
        Simulates a broker response and processes it through AI.
        """
        global MOCK_STATE

        if not AI_AVAILABLE:
            return {"error": "AI processing not available"}

        client_id = body.get('clientId', 'e2e-test-client')
        property_data = body.get('property', {})
        response_type = body.get('responseType', 'complete_info')

        if not property_data.get('address'):
            return {"error": "Property address required"}

        # Get response template
        template = BROKER_RESPONSES.get(response_type)
        if not template:
            return {"error": f"Unknown response type: {response_type}"}

        # Generate broker response
        contact_first = property_data.get('contact', '').split()[0] if property_data.get('contact') else 'Best'
        broker_response = template.format(
            address=property_data['address'],
            contact=contact_first
        )

        # Initialize property state
        property_key = f"{property_data['address']}_{property_data.get('city', '')}".lower().replace(' ', '_')

        if property_key not in MOCK_STATE['sheet_data']:
            row = [""] * len(SHEET_HEADER)
            row[0] = property_data.get('address', '')
            row[1] = property_data.get('city', '')
            row[4] = property_data.get('contact', '')
            row[5] = property_data.get('email', '')
            MOCK_STATE['sheet_data'][property_key] = row

        row = MOCK_STATE['sheet_data'][property_key]

        # Build conversation
        if property_key not in MOCK_STATE['threads']:
            MOCK_STATE['threads'][property_key] = [{
                "direction": "outbound",
                "content": f"Hi, I'm interested in {property_data['address']}. Could you provide availability and details?"
            }]

        MOCK_STATE['threads'][property_key].append({
            "direction": "inbound",
            "content": broker_response
        })

        # Build conversation payload
        conv_payload = []
        for i, msg in enumerate(MOCK_STATE['threads'][property_key]):
            conv_payload.append({
                "direction": msg["direction"],
                "from": property_data['email'] if msg["direction"] == "inbound" else "jill@company.com",
                "to": ["jill@company.com"] if msg["direction"] == "inbound" else [property_data['email']],
                "subject": f"{property_data['address']}, {property_data.get('city', '')}",
                "timestamp": f"2024-01-15T{10+i}:00:00Z",
                "preview": msg["content"][:200],
                "content": msg["content"]
            })

        # Call AI processing
        try:
            proposal = propose_sheet_updates(
                uid="e2e-test-user",
                client_id=client_id,
                email=property_data['email'],
                sheet_id="e2e-test-sheet",
                header=SHEET_HEADER,
                rownum=property_data.get('rowIndex', 3),
                rowvals=row,
                thread_id=f"thread-{property_key}",
                contact_name=property_data.get('contact', ''),
                conversation=conv_payload,
                dry_run=True
            )
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}

        # Process proposal
        notifications = []
        if proposal:
            # Apply updates
            header_map = {h.lower().strip(): i for i, h in enumerate(SHEET_HEADER)}
            for update in proposal.get('updates', []):
                col = update.get('column', '').lower().strip()
                if col in header_map:
                    idx = header_map[col]
                    row[idx] = update.get('value', '')

                notif = {
                    "kind": "sheet_update",
                    "column": update.get("column"),
                    "value": update.get("value"),
                    "address": property_data['address']
                }
                notifications.append(notif)
                MOCK_STATE['notifications'].append(notif)

            # Process events
            for event in proposal.get('events', []):
                event_type = event.get('type', '')

                if event_type == 'property_unavailable':
                    notif = {"kind": "property_unavailable", "address": property_data['address']}
                elif event_type == 'new_property':
                    notif = {
                        "kind": "action_needed",
                        "reason": "new_property_pending_approval",
                        "meta": {
                            "address": event.get("address"),
                            "contactName": event.get("contactName"),
                            "email": event.get("email")
                        }
                    }
                elif event_type == 'call_requested':
                    notif = {"kind": "action_needed", "reason": "call_requested"}
                elif event_type == 'tour_requested':
                    notif = {
                        "kind": "action_needed",
                        "reason": "tour_requested",
                        "meta": {"question": event.get("question", "")}
                    }
                elif event_type == 'needs_user_input':
                    reason = event.get('reason', 'unknown')
                    notif = {
                        "kind": "action_needed",
                        "reason": f"needs_user_input:{reason}",
                        "meta": {"question": event.get("question", "")}
                    }
                else:
                    continue

                notifications.append(notif)
                MOCK_STATE['notifications'].append(notif)

            # Check row completion
            required = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
            complete = sum(1 for f in required if row[header_map.get(f, 0)].strip())
            if complete == len(required):
                notif = {"kind": "row_completed", "address": property_data['address']}
                notifications.append(notif)
                MOCK_STATE['notifications'].append(notif)

            # Add AI response to conversation
            if proposal.get('response_email'):
                MOCK_STATE['threads'][property_key].append({
                    "direction": "outbound",
                    "content": proposal['response_email']
                })

        return {
            "success": True,
            "proposal": proposal,
            "notifications": notifications,
            "sheetRow": row,
            "conversationLength": len(MOCK_STATE['threads'].get(property_key, []))
        }


def run_server(port: int = 5002):
    """Run the E2E test server."""
    server = HTTPServer(('', port), E2ERequestHandler)
    print(f"🚀 E2E Test Server running on http://localhost:{port}")
    print(f"   Firestore Emulator: {os.environ.get('FIRESTORE_EMULATOR_HOST')}")
    print("")
    print("Endpoints:")
    print("  GET  /health              - Health check")
    print("  POST /process-outbox      - Process outbox emails")
    print("  POST /inject-reply        - Inject broker reply and process")
    print("  POST /process-message     - Process single message")
    print("  POST /process-user        - Full processing cycle")
    print("  POST /create-thread       - Create test thread for reply matching")
    print("")
    print("Campaign Simulation (for frontend E2E tests):")
    print("  POST /api/simulate-response - Simulate broker response with AI")
    print("  POST /api/campaign-grade    - Grade campaign results using quality_benchmark scoring")
    print("  POST /api/reset             - Reset test state")
    print("  GET  /api/campaign-state    - Get campaign state")
    print("  GET  /api/notifications     - Get all notifications")
    print("  GET  /api/property/<key>    - Get property details")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E Test Server")
    parser.add_argument("--port", type=int, default=5002, help="Port to run on")
    args = parser.parse_args()

    run_server(args.port)
