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

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.e2e_harness import E2EHarness, create_test_thread


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


def run_server(port: int = 5002):
    """Run the E2E test server."""
    server = HTTPServer(('', port), E2ERequestHandler)
    print(f"🚀 E2E Test Server running on http://localhost:{port}")
    print(f"   Firestore Emulator: {os.environ.get('FIRESTORE_EMULATOR_HOST')}")
    print("")
    print("Endpoints:")
    print("  GET  /health           - Health check")
    print("  POST /process-outbox   - Process outbox emails")
    print("  POST /inject-reply     - Inject broker reply and process")
    print("  POST /process-message  - Process single message")
    print("  POST /process-user     - Full processing cycle")
    print("  POST /create-thread    - Create test thread for reply matching")
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
