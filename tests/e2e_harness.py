#!/usr/bin/env python3
"""
E2E Test Harness for EmailAutomation Backend

This module enables running the REAL backend code against Firebase emulators,
with only email (Graph API) operations mocked. This allows frontend E2E tests
to trigger backend processing and verify the full pipeline.

Usage:
    # Set up emulators first
    export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080

    # From Python
    from tests.e2e_harness import E2EHarness
    harness = E2EHarness(user_id="test-user-123")
    harness.inject_broker_reply("broker@test.com", "The property has 15,000 SF...")
    harness.process_user()

    # From command line
    python tests/e2e_harness.py --user-id test-user-123 --action process
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from unittest.mock import patch, MagicMock

# CRITICAL: Set emulator environment BEFORE importing any Firebase modules
if os.environ.get('E2E_TEST_MODE') == 'true':
    os.environ['FIRESTORE_EMULATOR_HOST'] = os.environ.get('FIRESTORE_EMULATOR_HOST', '127.0.0.1:8080')
    print(f"🧪 E2E Test Mode: Using Firestore emulator at {os.environ['FIRESTORE_EMULATOR_HOST']}")


class MockGraphAPI:
    """Mock Microsoft Graph API for email operations."""

    def __init__(self):
        self.sent_emails: List[Dict] = []
        self.inbox_messages: List[Dict] = []
        self.draft_counter = 0

    def inject_inbox_message(self,
                              from_email: str,
                              from_name: str,
                              subject: str,
                              body: str,
                              in_reply_to: Optional[str] = None,
                              conversation_id: Optional[str] = None):
        """Inject a mock email into the inbox for processing."""
        self.draft_counter += 1
        msg_id = f"mock-msg-{self.draft_counter}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        internet_msg_id = f"<{msg_id}@mock.test>"

        message = {
            "id": msg_id,
            "subject": subject,
            "bodyPreview": body[:200] if body else "",
            "body": {"content": body, "contentType": "text"},
            "from": {
                "emailAddress": {
                    "address": from_email,
                    "name": from_name
                }
            },
            "toRecipients": [{"emailAddress": {"address": "test@example.com"}}],
            "receivedDateTime": datetime.now(timezone.utc).isoformat(),
            "sentDateTime": datetime.now(timezone.utc).isoformat(),
            "conversationId": conversation_id or f"conv-{msg_id}",
            "internetMessageId": internet_msg_id,
            "isRead": False,
            "internetMessageHeaders": []
        }

        if in_reply_to:
            message["internetMessageHeaders"].append({
                "name": "In-Reply-To",
                "value": in_reply_to
            })
            message["internetMessageHeaders"].append({
                "name": "References",
                "value": in_reply_to
            })

        self.inbox_messages.append(message)
        return message

    def mock_request(self, method: str, url: str, **kwargs) -> MagicMock:
        """Mock requests to Graph API."""
        response = MagicMock()
        response.status_code = 200

        # Handle different Graph API endpoints
        if "/me/mailFolders/Inbox/messages" in url and method == "get":
            # Return inbox messages
            response.json.return_value = {"value": self.inbox_messages}
            response.text = json.dumps({"value": self.inbox_messages})

        elif "/me/messages" in url and method == "post":
            # Create draft
            self.draft_counter += 1
            draft_id = f"draft-{self.draft_counter}"
            data = kwargs.get('json', {})

            response.json.return_value = {
                "id": draft_id,
                "internetMessageId": f"<{draft_id}@mock.test>",
                "conversationId": f"conv-{draft_id}"
            }
            response.text = json.dumps(response.json.return_value)

            # Record the sent email
            self.sent_emails.append({
                "id": draft_id,
                "to": [r.get("emailAddress", {}).get("address") for r in data.get("toRecipients", [])],
                "subject": data.get("subject"),
                "body": data.get("body", {}).get("content"),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        elif "/send" in url and method == "post":
            # Send email - just acknowledge
            response.status_code = 202
            response.text = ""

        elif "/me/messages/" in url and method == "get":
            # Get message details
            msg_id = url.split("/me/messages/")[1].split("?")[0]
            response.json.return_value = {
                "id": msg_id,
                "internetMessageId": f"<{msg_id}@mock.test>",
                "conversationId": f"conv-{msg_id}"
            }

        elif method == "patch":
            # Mark as read
            response.status_code = 200

        else:
            response.status_code = 404
            response.text = "Not found"

        return response


class MockOpenAI:
    """Mock OpenAI API for AI extraction operations."""

    def __init__(self):
        self.call_count = 0
        # Default mock response - simulates a basic extraction
        self.default_response = {
            "updates": [],
            "response": "Thank you for your response. I'll follow up with any additional questions.",
            "events": [],
            "response_type": "missing_fields"
        }

    def mock_completion(self, **kwargs):
        """Mock chat.completions.create response."""
        self.call_count += 1

        # Create a mock response object
        mock_message = MagicMock()
        mock_message.content = json.dumps(self.default_response)
        mock_message.refusal = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock()
        mock_response.usage.total_tokens = 100

        return mock_response

    def set_expected_response(self, response: Dict):
        """Set the response that should be returned for the next call."""
        self.default_response = response


class E2EHarness:
    """
    E2E Test Harness for running real backend code with mocked external APIs.

    This allows frontend E2E tests to:
    1. Seed data in Firestore (via the emulator)
    2. Inject mock broker replies
    3. Trigger backend processing
    4. Verify notifications and sheet updates in Firestore
    """

    def __init__(self, user_id: str, mock_openai: bool = True):
        self.user_id = user_id
        self.mock_graph = MockGraphAPI()
        self.mock_openai_enabled = mock_openai and not os.environ.get('OPENAI_API_KEY')
        self.mock_openai = MockOpenAI() if self.mock_openai_enabled else None
        self._patches = []

    def __enter__(self):
        self._start_mocks()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_mocks()

    def _start_mocks(self):
        """Start mocking external APIs."""
        import requests

        # Mock requests.get and requests.post for Graph API
        def mock_request(method):
            original = getattr(requests, method)
            def wrapper(url, *args, **kwargs):
                if "graph.microsoft.com" in url:
                    return self.mock_graph.mock_request(method, url, **kwargs)
                return original(url, *args, **kwargs)
            return wrapper

        self._patches.append(patch('requests.get', mock_request('get')))
        self._patches.append(patch('requests.post', mock_request('post')))
        self._patches.append(patch('requests.patch', mock_request('patch')))

        # Mock OpenAI if enabled (no API key available)
        if self.mock_openai_enabled:
            print("🤖 Mocking OpenAI API (no OPENAI_API_KEY set)")
            # Mock the OpenAI client's chat.completions.create method
            self._patches.append(patch(
                'email_automation.ai_processing.client.chat.completions.create',
                side_effect=self.mock_openai.mock_completion
            ))

        for p in self._patches:
            p.start()

    def _stop_mocks(self):
        """Stop mocking."""
        for p in self._patches:
            p.stop()
        self._patches.clear()

    def inject_broker_reply(self,
                            from_email: str,
                            body: str,
                            from_name: str = "Test Broker",
                            subject: str = "RE: Property Inquiry",
                            in_reply_to: Optional[str] = None,
                            conversation_id: Optional[str] = None) -> Dict:
        """
        Inject a mock broker reply into the "inbox" for processing.

        Args:
            from_email: Broker's email address
            body: Email body text (broker's response)
            from_name: Broker's display name
            subject: Email subject
            in_reply_to: Internet-Message-ID of the email being replied to
            conversation_id: Microsoft conversation ID for threading

        Returns:
            The mock message object
        """
        return self.mock_graph.inject_inbox_message(
            from_email=from_email,
            from_name=from_name,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            conversation_id=conversation_id
        )

    def get_sent_emails(self) -> List[Dict]:
        """Get all emails that were "sent" during processing."""
        return self.mock_graph.sent_emails

    def set_expected_ai_response(self, response: Dict):
        """
        Set the expected AI response for the next processing call.

        Args:
            response: Dict with keys: updates, response, events, response_type

        Example:
            harness.set_expected_ai_response({
                "updates": [{"column": "Total SF", "value": "15000"}],
                "response": "Thank you for the information.",
                "events": [],
                "response_type": "missing_fields"
            })
        """
        if self.mock_openai:
            self.mock_openai.set_expected_response(response)
        else:
            print("⚠️ Cannot set AI response - using real OpenAI API")

    def process_outbox(self) -> Dict:
        """
        Process the user's outbox - sends queued emails.

        This runs the REAL send_outboxes() function with Graph API mocked.
        """
        from email_automation.email import send_outboxes

        # Create mock auth headers (Graph API is mocked anyway)
        headers = {
            "Authorization": "Bearer mock-token-for-testing",
            "Content-Type": "application/json"
        }

        with self:
            return send_outboxes(self.user_id, headers)

    def process_inbox(self) -> List[Dict]:
        """
        Process the inbox - finds replies and extracts data.

        This runs the REAL scan_inbox_against_index() function.
        """
        from email_automation.processing import scan_inbox_against_index

        headers = {
            "Authorization": "Bearer mock-token-for-testing",
            "Content-Type": "application/json"
        }

        with self:
            return scan_inbox_against_index(self.user_id, headers)

    def process_single_message(self, message: Dict) -> Dict:
        """
        Process a single inbox message through AI extraction.

        Args:
            message: The message dict (from inject_broker_reply or inbox)

        Returns:
            Processing result with extracted data and notifications
        """
        from email_automation.processing import process_inbox_message

        headers = {
            "Authorization": "Bearer mock-token-for-testing",
            "Content-Type": "application/json"
        }

        with self:
            return process_inbox_message(self.user_id, headers, message)

    def process_user(self) -> Dict:
        """
        Run the full processing cycle for this user.

        This is equivalent to what GitHub Actions runs every 30 minutes.
        Calls send_outboxes → scan_inbox → check_followups.
        """
        from main import refresh_and_process_user

        with self:
            return refresh_and_process_user(self.user_id)


def create_test_thread(user_id: str,
                       client_id: str,
                       property_address: str,
                       broker_email: str,
                       internet_message_id: str,
                       conversation_id: str) -> str:
    """
    Create a thread in Firestore for testing reply matching.

    This sets up the data structure that the backend expects when
    matching incoming replies to existing threads.
    """
    from google.cloud import firestore

    db = firestore.Client()

    # Create thread document
    thread_ref = db.collection("users").document(user_id).collection("threads").document()
    thread_data = {
        "clientId": client_id,
        "email": [broker_email],
        "property": {
            "address": property_address,
            "rowIndex": 3  # Default row index
        },
        "conversationId": conversation_id,
        "hasUnprocessedReply": False,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "lastProcessedAt": firestore.SERVER_TIMESTAMP
    }
    thread_ref.set(thread_data)

    # Create message index for reply matching
    msg_index_ref = db.collection("users").document(user_id).collection("msgIndex").document(internet_message_id)
    msg_index_ref.set({
        "threadId": thread_ref.id,
        "createdAt": firestore.SERVER_TIMESTAMP
    })

    # Create conversation index
    conv_index_ref = db.collection("users").document(user_id).collection("convIndex").document(conversation_id)
    conv_index_ref.set({
        "threadId": thread_ref.id,
        "createdAt": firestore.SERVER_TIMESTAMP
    })

    print(f"✅ Created test thread {thread_ref.id} for {property_address}")
    return thread_ref.id


def main():
    """CLI interface for E2E harness."""
    parser = argparse.ArgumentParser(description="E2E Test Harness for EmailAutomation")
    parser.add_argument("--user-id", required=True, help="User ID to process")
    parser.add_argument("--action", choices=["process", "outbox", "inbox"],
                       default="process", help="Action to perform")
    parser.add_argument("--inject-reply", help="JSON file with mock reply to inject")

    args = parser.parse_args()

    harness = E2EHarness(args.user_id)

    if args.inject_reply:
        with open(args.inject_reply) as f:
            reply_data = json.load(f)
        msg = harness.inject_broker_reply(**reply_data)
        print(f"✅ Injected reply: {msg['id']}")

    if args.action == "process":
        result = harness.process_user()
        print(f"✅ Processed user: {json.dumps(result, indent=2, default=str)}")
    elif args.action == "outbox":
        result = harness.process_outbox()
        print(f"✅ Processed outbox: {json.dumps(result, indent=2, default=str)}")
    elif args.action == "inbox":
        result = harness.process_inbox()
        print(f"✅ Processed inbox: {len(result)} messages")


if __name__ == "__main__":
    # Enable test mode
    os.environ['E2E_TEST_MODE'] = 'true'
    main()
