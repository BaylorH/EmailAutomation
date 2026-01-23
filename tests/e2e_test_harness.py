"""
E2E Test Harness for Full Production Simulation

This module patches all external service clients to use in-memory mocks,
enabling complete E2E testing of the production pipeline without any
external API calls.

Usage:
    from tests.e2e_test_harness import TestHarness

    with TestHarness() as harness:
        # All email_automation code now uses mocks
        harness.inject_email(...)
        harness.setup_sheet(...)

        # Run production code
        from email_automation.processing import process_incoming_email
        process_incoming_email(...)

        # Verify results
        assert harness.sheets.get_cell(...) == expected
        assert len(harness.firestore.write_log) > 0
"""

import os
import sys

# Set dummy environment variables BEFORE importing any production modules
os.environ.setdefault("AZURE_API_APP_ID", "test_app_id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test_secret")
os.environ.setdefault("FIREBASE_API_KEY", "test_firebase_key")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test_google_id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test_google_secret")
os.environ.setdefault("GOOGLE_REFRESH_TOKEN", "test_refresh_token")

# Mock google.cloud.firestore BEFORE it gets imported by clients.py
from unittest.mock import MagicMock
import unittest.mock as mock

# Create a mock firestore module
mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"

# Inject it into sys.modules before any imports
sys.modules['google.cloud.firestore'] = mock_firestore
sys.modules['google.cloud'] = MagicMock()
sys.modules['google.cloud'].firestore = mock_firestore

# Also mock openai before import
mock_openai = MagicMock()
sys.modules['openai'] = mock_openai

# Mock googleapiclient
mock_googleapiclient = MagicMock()
sys.modules['googleapiclient'] = mock_googleapiclient
sys.modules['googleapiclient.discovery'] = MagicMock()
sys.modules['googleapiclient.http'] = MagicMock()

# Mock google.oauth2.credentials
sys.modules['google.oauth2'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google.auth'] = MagicMock()
sys.modules['google.auth.transport'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()

import json
import uuid
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Callable
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass, field
from collections import defaultdict
from contextlib import contextmanager

# Import mock services
from tests.mock_services import (
    MockEmailProvider, MockSheetsProvider, MockFirestoreProvider,
    MockDriveProvider, MockOpenAIProvider, create_mock_services
)


class MockFirestoreClient:
    """Mock Firestore client that mimics google.cloud.firestore.Client interface."""

    def __init__(self, provider: MockFirestoreProvider):
        self.provider = provider
        self._path_stack = []

    def collection(self, name: str):
        """Return a collection reference."""
        return MockCollectionRef(self.provider, name)

    def transaction(self):
        """Return a mock transaction."""
        return MockTransaction(self.provider)


class MockCollectionRef:
    """Mock Firestore collection reference."""

    def __init__(self, provider: MockFirestoreProvider, path: str):
        self.provider = provider
        self.path = path

    def document(self, doc_id: str):
        """Return a document reference."""
        return MockDocumentRef(self.provider, f"{self.path}/{doc_id}")

    def where(self, *args):
        """Return a query."""
        if len(args) == 3:
            field, op, value = args
            return MockQuery(self.provider, self.path, [(field, op, value)])
        else:
            # FieldFilter style
            field_filter = args[0]
            return MockQuery(self.provider, self.path, [
                (field_filter.field_path, field_filter.op_string, field_filter.value)
            ])

    def order_by(self, field: str, direction: str = "ASCENDING"):
        """Return a query with ordering."""
        return MockQuery(self.provider, self.path, [], order_by=field)

    def limit(self, count: int):
        """Return a query with limit."""
        return MockQuery(self.provider, self.path, [], limit=count)

    def stream(self):
        """Stream all documents in collection."""
        docs = self.provider.list_subcollection(self.path)
        return [MockDocumentSnapshot(d.id, d.data, d.exists) for d in docs]

    def get(self):
        """Get all documents in collection."""
        return self.stream()


class MockDocumentRef:
    """Mock Firestore document reference."""

    def __init__(self, provider: MockFirestoreProvider, path: str):
        self.provider = provider
        self.path = path
        self.id = path.split("/")[-1]

    def collection(self, name: str):
        """Return a subcollection reference."""
        return MockCollectionRef(self.provider, f"{self.path}/{name}")

    def get(self, transaction=None):
        """Get the document."""
        doc = self.provider.get_document(self.path)
        return MockDocumentSnapshot(doc.id, doc.data, doc.exists)

    def set(self, data: Dict, merge: bool = False):
        """Set document data."""
        # Handle SERVER_TIMESTAMP
        processed_data = self._process_timestamps(data)
        self.provider.set_document(self.path, processed_data, merge=merge)

    def update(self, data: Dict):
        """Update document fields."""
        processed_data = self._process_timestamps(data)
        try:
            self.provider.update_document(self.path, processed_data)
        except ValueError:
            # Document doesn't exist, create it
            self.provider.set_document(self.path, processed_data, merge=False)

    def delete(self):
        """Delete the document."""
        self.provider.delete_document(self.path)

    def _process_timestamps(self, data: Dict) -> Dict:
        """Replace SERVER_TIMESTAMP sentinels with actual timestamps."""
        from google.cloud.firestore import SERVER_TIMESTAMP
        result = {}
        for key, value in data.items():
            if value is SERVER_TIMESTAMP:
                result[key] = datetime.now(timezone.utc)
            elif isinstance(value, dict):
                result[key] = self._process_timestamps(value)
            else:
                result[key] = value
        return result


class MockDocumentSnapshot:
    """Mock Firestore document snapshot."""

    def __init__(self, doc_id: str, data: Dict, exists: bool):
        self.id = doc_id
        self._data = data
        self.exists = exists

    def to_dict(self):
        """Return document data as dict."""
        return self._data.copy() if self._data else None

    def get(self, field: str):
        """Get a specific field."""
        return self._data.get(field) if self._data else None


class MockQuery:
    """Mock Firestore query."""

    def __init__(self, provider: MockFirestoreProvider, path: str,
                 filters: List[tuple] = None, order_by: str = None, limit: int = None):
        self.provider = provider
        self.path = path
        self.filters = filters or []
        self._order_by = order_by
        self._limit = limit

    def where(self, *args):
        """Add a filter."""
        if len(args) == 3:
            field, op, value = args
        else:
            # New style with FieldFilter
            field_filter = args[0]
            field = field_filter.field_path
            op = field_filter.op_string
            value = field_filter.value
        new_filters = self.filters + [(field, op, value)]
        return MockQuery(self.provider, self.path, new_filters, self._order_by, self._limit)

    def order_by(self, field: str, direction: str = "ASCENDING"):
        """Add ordering."""
        order = f"-{field}" if direction == "DESCENDING" else field
        return MockQuery(self.provider, self.path, self.filters, order, self._limit)

    def limit(self, count: int):
        """Add limit."""
        return MockQuery(self.provider, self.path, self.filters, self._order_by, count)

    def stream(self):
        """Execute query and stream results."""
        docs = self.provider.query_collection(
            self.path,
            filters=self.filters,
            order_by=self._order_by,
            limit=self._limit
        )
        return [MockDocumentSnapshot(d.id, d.data, d.exists) for d in docs]

    def get(self):
        """Execute query and get results."""
        return self.stream()


class MockTransaction:
    """Mock Firestore transaction."""

    def __init__(self, provider: MockFirestoreProvider):
        self.provider = provider
        self._operations = []

    def get(self, doc_ref):
        """Get document in transaction."""
        return doc_ref.get()

    def set(self, doc_ref, data, merge=False):
        """Set document in transaction."""
        doc_ref.set(data, merge=merge)

    def update(self, doc_ref, data):
        """Update document in transaction."""
        doc_ref.update(data)

    def delete(self, doc_ref):
        """Delete document in transaction."""
        doc_ref.delete()


class MockSheetsService:
    """Mock Google Sheets API service."""

    def __init__(self, provider: MockSheetsProvider):
        self.provider = provider

    def spreadsheets(self):
        return MockSpreadsheets(self.provider)


class MockSpreadsheets:
    """Mock spreadsheets resource."""

    def __init__(self, provider: MockSheetsProvider):
        self.provider = provider

    def values(self):
        return MockValues(self.provider)

    def get(self, spreadsheetId: str):
        return MockRequest(lambda: self.provider.get_sheet_metadata(spreadsheetId))

    def batchUpdate(self, spreadsheetId: str, body: Dict):
        # Handle formatting requests (no-op for tests)
        return MockRequest(lambda: {"replies": []})


class MockValues:
    """Mock spreadsheets.values resource."""

    def __init__(self, provider: MockSheetsProvider):
        self.provider = provider

    def get(self, spreadsheetId: str, range: str):
        return MockRequest(lambda: {"values": self.provider.get_values(spreadsheetId, range)})

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: Dict):
        return MockRequest(lambda: self.provider.update_values(
            spreadsheetId, range, body.get("values", []), valueInputOption
        ))

    def batchUpdate(self, spreadsheetId: str, body: Dict):
        return MockRequest(lambda: self.provider.batch_update_values(
            spreadsheetId, body.get("data", [])
        ))

    def append(self, spreadsheetId: str, range: str, valueInputOption: str, body: Dict):
        return MockRequest(lambda: self.provider.append_values(
            spreadsheetId, range, body.get("values", []), valueInputOption
        ))


class MockRequest:
    """Mock Google API request."""

    def __init__(self, executor: Callable):
        self._executor = executor

    def execute(self):
        return self._executor()


class MockDriveService:
    """Mock Google Drive API service."""

    def __init__(self, provider: MockDriveProvider):
        self.provider = provider

    def files(self):
        return MockFiles(self.provider)

    def permissions(self):
        return MockPermissions(self.provider)


class MockFiles:
    """Mock drive.files resource."""

    def __init__(self, provider: MockDriveProvider):
        self.provider = provider

    def list(self, q: str = None, spaces: str = None, fields: str = None, pageSize: int = 10):
        return MockRequest(lambda: {"files": self.provider.list_files(q or "", pageSize)})

    def create(self, body: Dict = None, media_body=None, fields: str = None):
        if body.get("mimeType") == "application/vnd.google-apps.folder":
            return MockRequest(lambda: self.provider.create_folder(
                body.get("name", "Untitled"),
                body.get("parents", [None])[0] if body.get("parents") else None
            ))
        else:
            content = media_body._fd.read() if hasattr(media_body, '_fd') else b""
            return MockRequest(lambda: self.provider.upload_file(
                body.get("name", "Untitled"),
                content,
                body.get("mimeType", "application/octet-stream"),
                body.get("parents", [None])[0] if body.get("parents") else None
            ))


class MockPermissions:
    """Mock drive.permissions resource."""

    def __init__(self, provider: MockDriveProvider):
        self.provider = provider

    def create(self, fileId: str, body: Dict):
        return MockRequest(lambda: {"id": "permission_" + fileId})


class TestHarness:
    """
    Context manager that patches all external services with mocks.

    Example:
        with TestHarness() as harness:
            # Setup test data
            harness.setup_sheet("sheet123", ["Email", "Name"], [["test@test.com", "John"]])
            harness.inject_email("broker@test.com", "Broker", "RE: Property", "Here is the info...")

            # Configure AI responses
            harness.set_ai_response(lambda msgs, *args: json.dumps({
                "updates": [{"column": "Total SF", "value": "10000"}],
                "events": [],
                "response_email": "Thanks!"
            }))

            # Run production code
            from email_automation.processing import inbox_scan_for_each_user
            inbox_scan_for_each_user()

            # Verify
            assert harness.sheets.get_cell("sheet123", "Sheet1", 1, 2) == "10000"
    """

    def __init__(self, ai_response_generator: Callable = None):
        self.mocks = create_mock_services(ai_response_generator)
        self.email: MockEmailProvider = self.mocks["email"]
        self.sheets: MockSheetsProvider = self.mocks["sheets"]
        self.firestore: MockFirestoreProvider = self.mocks["firestore"]
        self.drive: MockDriveProvider = self.mocks["drive"]
        self.openai: MockOpenAIProvider = self.mocks["openai"]

        self._patches = []
        self._mock_fs = MockFirestoreClient(self.firestore)
        self._mock_sheets = MockSheetsService(self.sheets)
        self._mock_drive = MockDriveService(self.drive)

    def __enter__(self):
        """Start patching."""
        # Patch the main source module - clients._fs
        # This is the canonical source that other modules import from
        self._patches.append(patch('email_automation.clients._fs', self._mock_fs))

        # Patch modules that import _fs at the top level (before function calls)
        self._patches.append(patch('email_automation.processing._fs', self._mock_fs))
        self._patches.append(patch('email_automation.messaging._fs', self._mock_fs))
        self._patches.append(patch('email_automation.notifications._fs', self._mock_fs))
        self._patches.append(patch('email_automation.ai_processing._fs', self._mock_fs))
        self._patches.append(patch('email_automation.sheet_operations._fs', self._mock_fs))
        self._patches.append(patch('email_automation.logging._fs', self._mock_fs))
        self._patches.append(patch('email_automation.email_operations._fs', self._mock_fs))

        # Patch Sheets client (the function that returns sheets service)
        self._patches.append(patch('email_automation.clients._sheets_client', lambda: self._mock_sheets))
        self._patches.append(patch('email_automation.sheets._sheets_client', lambda: self._mock_sheets))
        self._patches.append(patch('email_automation.processing._sheets_client', lambda: self._mock_sheets))
        self._patches.append(patch('email_automation.ai_processing._sheets_client', lambda: self._mock_sheets))
        self._patches.append(patch('email_automation.sheet_operations._sheets_client', lambda: self._mock_sheets))
        self._patches.append(patch('email_automation.logging._sheets_client', lambda: self._mock_sheets))

        # Patch Drive - only needed in file_handling
        # Note: build is from googleapiclient.discovery, we already mocked that module

        # Patch OpenAI client
        self._patches.append(patch('email_automation.clients.client', self._create_mock_openai_client()))
        self._patches.append(patch('email_automation.ai_processing.client', self._create_mock_openai_client()))

        # Patch requests for Graph API
        self._patches.append(patch('email_automation.email.requests', self._create_mock_requests()))
        self._patches.append(patch('email_automation.processing.requests', self._create_mock_requests()))
        self._patches.append(patch('email_automation.email_operations.requests', self._create_mock_requests()))
        self._patches.append(patch('email_automation.utils.requests', self._create_mock_requests()))

        # Start all patches, ignoring ones that fail (module may not have the attribute)
        for p in self._patches:
            try:
                p.start()
            except AttributeError:
                pass  # Module doesn't have this attribute, skip

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop patching."""
        for p in self._patches:
            p.stop()
        return False

    def _create_mock_openai_client(self):
        """Create a mock OpenAI client."""
        mock = MagicMock()

        def chat_completion_create(**kwargs):
            messages = kwargs.get("messages", [])
            model = kwargs.get("model")
            temperature = kwargs.get("temperature", 0.3)
            response_format = kwargs.get("response_format")

            content = self.openai.chat_completion(messages, model, temperature, response_format)

            # Create mock response structure
            response = MagicMock()
            response.choices = [MagicMock()]
            response.choices[0].message.content = content
            return response

        mock.chat.completions.create = chat_completion_create
        return mock

    def _create_mock_requests(self):
        """Create a mock requests module for Graph API."""
        mock = MagicMock()

        def mock_get(url, **kwargs):
            return self._handle_graph_request("GET", url, kwargs)

        def mock_post(url, **kwargs):
            return self._handle_graph_request("POST", url, kwargs)

        mock.get = mock_get
        mock.post = mock_post
        mock.exceptions = MagicMock()
        mock.exceptions.RequestException = Exception

        return mock

    def _handle_graph_request(self, method: str, url: str, kwargs: Dict):
        """Handle Microsoft Graph API requests."""
        response = MagicMock()
        response.status_code = 200

        # Parse URL
        if "/me/messages" in url and method == "GET":
            if "internetMessageId eq" in kwargs.get("params", {}).get("$filter", ""):
                # Lookup by internet message ID
                filter_val = kwargs["params"]["$filter"]
                match = re.search(r"eq '([^']+)'", filter_val)
                if match:
                    msg_id = self.email.lookup_message_by_internet_id(match.group(1))
                    if msg_id:
                        response.json = lambda: {"value": [{"id": msg_id}]}
                    else:
                        response.json = lambda: {"value": []}
            elif "/messages/" in url:
                # Get specific message
                msg_id = url.split("/messages/")[1].split("?")[0].split("/")[0]
                try:
                    msg = self.email.get_message(msg_id)
                    response.json = lambda m=msg: {
                        "id": m.id,
                        "internetMessageId": m.internet_message_id,
                        "conversationId": m.conversation_id,
                        "subject": m.subject,
                        "body": {"content": m.body},
                        "bodyPreview": m.body_preview,
                        "from": {"emailAddress": {"address": m.from_address, "name": m.from_name}},
                        "toRecipients": [{"emailAddress": {"address": r}} for r in m.to_recipients],
                        "ccRecipients": [{"emailAddress": {"address": r}} for r in m.cc_recipients],
                        "receivedDateTime": m.received_datetime.isoformat() if m.received_datetime else None,
                        "hasAttachments": m.has_attachments
                    }
                except ValueError:
                    response.status_code = 404
                    response.json = lambda: {"error": "Not found"}
            else:
                # List messages
                messages = self.email.list_messages()
                response.json = lambda: {"value": [
                    {
                        "id": m.id,
                        "internetMessageId": m.internet_message_id,
                        "conversationId": m.conversation_id,
                        "subject": m.subject,
                        "bodyPreview": m.body_preview,
                        "from": {"emailAddress": {"address": m.from_address, "name": m.from_name}},
                        "toRecipients": [{"emailAddress": {"address": r}} for r in m.to_recipients],
                        "receivedDateTime": m.received_datetime.isoformat() if m.received_datetime else None,
                        "hasAttachments": m.has_attachments
                    } for m in messages
                ]}

        elif "/me/messages" in url and method == "POST":
            if "/send" in url:
                # Send draft
                draft_id = url.split("/messages/")[1].split("/send")[0]
                self.email.send_draft(draft_id)
                response.status_code = 202
                response.json = lambda: {}
            elif "/reply" in url:
                # Reply to message
                msg_id = url.split("/messages/")[1].split("/reply")[0]
                body = kwargs.get("json", {}).get("message", {}).get("body", {}).get("content", "")
                self.email.reply_to_message(msg_id, body)
                response.status_code = 202
                response.json = lambda: {}
            else:
                # Create draft
                payload = kwargs.get("json", {})
                result = self.email.create_draft(
                    subject=payload.get("subject", ""),
                    body=payload.get("body", {}).get("content", ""),
                    to_recipients=[r["emailAddress"]["address"] for r in payload.get("toRecipients", [])]
                )
                response.json = lambda r=result: r

        elif "/me/sendMail" in url and method == "POST":
            # Send new mail
            payload = kwargs.get("json", {}).get("message", {})
            self.email.send_new_message(
                subject=payload.get("subject", ""),
                body=payload.get("body", {}).get("content", ""),
                to_recipients=[r["emailAddress"]["address"] for r in payload.get("toRecipients", [])]
            )
            response.status_code = 202
            response.json = lambda: {}

        response.raise_for_status = lambda: None
        return response

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def setup_sheet(self, sheet_id: str, headers: List[str], rows: List[List[Any]] = None,
                    sheet_name: str = "Sheet1"):
        """Set up a mock spreadsheet with headers and optional data rows."""
        self.sheets.create_spreadsheet(sheet_id, sheet_name, headers, rows)

    def inject_email(self, from_address: str, from_name: str, subject: str, body: str,
                     to_recipients: List[str] = None, conversation_id: str = None):
        """Inject an incoming email into the mock inbox."""
        return self.email.inject_incoming_email(
            from_address=from_address,
            from_name=from_name,
            subject=subject,
            body=body,
            to_recipients=to_recipients,
            conversation_id=conversation_id
        )

    def setup_client(self, user_id: str, client_id: str, client_name: str, sheet_id: str,
                     emails: List[str] = None, criteria: str = None):
        """Set up a client in mock Firestore."""
        client_data = {
            "name": client_name,
            "sheetId": sheet_id,
            "emails": emails or [],
            "criteria": criteria or "",
            "createdAt": datetime.now(timezone.utc),
            "status": "active"
        }
        self.firestore.set_document(f"users/{user_id}/clients/{client_id}", client_data)

    def setup_user(self, user_id: str, signature: str = None):
        """Set up a user in mock Firestore."""
        user_data = {
            "createdAt": datetime.now(timezone.utc),
            "signatureMode": "use_full_signature" if signature else "none",
            "fullSignature": signature or ""
        }
        self.firestore.set_document(f"users/{user_id}", user_data)

    def setup_thread(self, user_id: str, thread_id: str, client_id: str, property_address: str,
                     row_number: int, internet_message_id: str = None, conversation_id: str = None):
        """Set up an existing thread in mock Firestore."""
        from tests.mock_services import generate_message_id, generate_conversation_id

        internet_msg_id = internet_message_id or generate_message_id()
        conv_id = conversation_id or generate_conversation_id()

        thread_data = {
            "clientId": client_id,
            "propertyAddress": property_address,
            "rowNumber": row_number,
            "internetMessageId": internet_msg_id,
            "conversationId": conv_id,
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc)
        }
        self.firestore.set_document(f"users/{user_id}/threads/{thread_id}", thread_data)

        # Index for lookup
        from email_automation.utils import b64url_id
        encoded_id = b64url_id(internet_msg_id)
        self.firestore.set_document(f"users/{user_id}/msgIndex/{encoded_id}", {"threadId": thread_id})
        self.firestore.set_document(f"users/{user_id}/convIndex/{conv_id}", {"threadId": thread_id})

        return internet_msg_id, conv_id

    def set_ai_response(self, generator: Callable):
        """Set the AI response generator function."""
        self.openai.response_generator = generator

    def set_ai_response_json(self, response: Dict):
        """Set a static AI response (as JSON object)."""
        self.openai.response_generator = lambda *args, **kwargs: json.dumps(response)

    def get_sheet_cell(self, sheet_id: str, row: int, col: int, sheet_name: str = "Sheet1") -> Any:
        """Get a cell value from mock sheet (0-indexed)."""
        return self.sheets.get_cell(sheet_id, sheet_name, row, col)

    def get_sheet_row(self, sheet_id: str, row: int, sheet_name: str = "Sheet1") -> List[Any]:
        """Get a row from mock sheet (0-indexed)."""
        if sheet_id not in self.sheets.spreadsheets:
            return []
        if sheet_name not in self.sheets.spreadsheets[sheet_id]:
            return []
        data = self.sheets.spreadsheets[sheet_id][sheet_name]
        if row >= len(data):
            return []
        return data[row]

    def get_sent_emails(self) -> List[Dict]:
        """Get all sent emails (including drafts that were sent)."""
        return self.email.send_log + self.email.reply_log

    def get_notifications(self, user_id: str, client_id: str = None) -> List[Dict]:
        """Get notifications from mock Firestore."""
        if client_id:
            path = f"users/{user_id}/clients/{client_id}/notifications"
        else:
            path = f"users/{user_id}/notifications"
        docs = self.firestore.list_subcollection(path)
        return [d.data for d in docs]

    def reset(self):
        """Reset all mocks to initial state."""
        self.email.clear()
        self.sheets.clear()
        self.firestore.clear()
        self.drive.clear()
        self.openai.clear()
