"""
Mock Service Implementations for E2E Testing

These mock implementations simulate all external services (Email, Sheets, Firestore,
Drive, OpenAI) using in-memory data stores. This allows testing the entire production
pipeline without any external API calls.

Usage:
    from tests.mock_services import create_mock_services, MockEmailProvider
    from email_automation.service_providers import set_test_mode

    mocks = create_mock_services()
    set_test_mode(True, mock_services=mocks)

    # Now all code using get_provider() will use mocks
"""

import json
import uuid
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from collections import defaultdict

from email_automation.service_providers import (
    EmailProvider, SheetsProvider, FirestoreProvider, DriveProvider, OpenAIProvider,
    EmailMessage, FirestoreDocument,
    generate_id, generate_message_id, generate_conversation_id
)


# =============================================================================
# MOCK EMAIL PROVIDER
# =============================================================================

class MockEmailProvider(EmailProvider):
    """
    In-memory mock for Microsoft Graph email operations.

    Features:
    - Stores all emails in memory with full threading support
    - Simulates inbox/sent folder behavior
    - Tracks all send operations for verification
    - Can inject "incoming" emails for testing
    """

    def __init__(self):
        self.inbox: List[EmailMessage] = []
        self.sent: List[EmailMessage] = []
        self.drafts: Dict[str, EmailMessage] = {}

        # For tracking/verification
        self.send_log: List[Dict[str, Any]] = []
        self.reply_log: List[Dict[str, Any]] = []

        # Conversation tracking
        self.conversations: Dict[str, List[EmailMessage]] = defaultdict(list)

    def inject_incoming_email(self, from_address: str, from_name: str, subject: str,
                              body: str, to_recipients: List[str] = None,
                              conversation_id: str = None,
                              in_reply_to: str = None) -> EmailMessage:
        """Inject a simulated incoming email into the inbox."""
        msg_id = generate_id("msg_")
        internet_msg_id = generate_message_id()
        conv_id = conversation_id or generate_conversation_id()

        msg = EmailMessage(
            id=msg_id,
            internet_message_id=internet_msg_id,
            conversation_id=conv_id,
            subject=subject,
            body=body,
            body_preview=body[:200] if body else "",
            from_address=from_address,
            from_name=from_name,
            to_recipients=to_recipients or ["user@example.com"],
            cc_recipients=[],
            received_datetime=datetime.utcnow(),
            has_attachments=False,
            attachments=[]
        )

        self.inbox.append(msg)
        self.conversations[conv_id].append(msg)
        return msg

    def list_messages(self, folder: str = "inbox", filter_query: str = None,
                      top: int = 50) -> List[EmailMessage]:
        """List messages from inbox or sent."""
        messages = self.inbox if folder == "inbox" else self.sent

        if filter_query:
            # Simple filter parsing for common queries
            if "from/emailAddress/address eq" in filter_query:
                match = re.search(r"eq '([^']+)'", filter_query)
                if match:
                    addr = match.group(1).lower()
                    messages = [m for m in messages if addr in m.from_address.lower()]
            elif "receivedDateTime ge" in filter_query:
                # Skip date filtering for tests - return all
                pass

        # Sort by received date descending
        messages = sorted(messages, key=lambda m: m.received_datetime or datetime.min, reverse=True)
        return messages[:top]

    def get_message(self, message_id: str) -> EmailMessage:
        """Get a single message by ID."""
        for msg in self.inbox + self.sent + list(self.drafts.values()):
            if msg.id == message_id:
                return msg
        raise ValueError(f"Message not found: {message_id}")

    def create_draft(self, subject: str, body: str, to_recipients: List[str],
                     cc_recipients: List[str] = None,
                     headers: Dict[str, str] = None) -> Dict[str, str]:
        """Create a draft message."""
        draft_id = generate_id("draft_")
        internet_msg_id = generate_message_id()
        conv_id = generate_conversation_id()

        msg = EmailMessage(
            id=draft_id,
            internet_message_id=internet_msg_id,
            conversation_id=conv_id,
            subject=subject,
            body=body,
            body_preview=body[:200] if body else "",
            from_address="user@example.com",
            from_name="Test User",
            to_recipients=to_recipients,
            cc_recipients=cc_recipients or [],
            sent_datetime=None,
            has_attachments=False
        )

        self.drafts[draft_id] = msg
        return {
            "id": draft_id,
            "internetMessageId": internet_msg_id,
            "conversationId": conv_id
        }

    def send_draft(self, draft_id: str) -> bool:
        """Send a draft message."""
        if draft_id not in self.drafts:
            return False

        msg = self.drafts.pop(draft_id)
        msg.sent_datetime = datetime.utcnow()
        self.sent.append(msg)
        self.conversations[msg.conversation_id].append(msg)

        self.send_log.append({
            "type": "draft_send",
            "message_id": msg.id,
            "internet_message_id": msg.internet_message_id,
            "to": msg.to_recipients,
            "subject": msg.subject,
            "body": msg.body,
            "timestamp": datetime.utcnow().isoformat()
        })
        return True

    def reply_to_message(self, message_id: str, body: str) -> bool:
        """Reply to a message in-thread."""
        # Find original message
        original = None
        for msg in self.inbox + self.sent:
            if msg.id == message_id:
                original = msg
                break

        if not original:
            return False

        reply_id = generate_id("reply_")
        internet_msg_id = generate_message_id()

        reply = EmailMessage(
            id=reply_id,
            internet_message_id=internet_msg_id,
            conversation_id=original.conversation_id,
            subject=f"Re: {original.subject}",
            body=body,
            body_preview=body[:200] if body else "",
            from_address="user@example.com",
            from_name="Test User",
            to_recipients=[original.from_address],
            sent_datetime=datetime.utcnow()
        )

        self.sent.append(reply)
        self.conversations[original.conversation_id].append(reply)

        self.reply_log.append({
            "type": "reply",
            "original_message_id": message_id,
            "reply_id": reply_id,
            "to": original.from_address,
            "body": body,
            "conversation_id": original.conversation_id,
            "timestamp": datetime.utcnow().isoformat()
        })
        return True

    def send_new_message(self, subject: str, body: str, to_recipients: List[str],
                         cc_recipients: List[str] = None) -> bool:
        """Send a new message (not in-thread)."""
        msg_id = generate_id("msg_")
        internet_msg_id = generate_message_id()
        conv_id = generate_conversation_id()

        msg = EmailMessage(
            id=msg_id,
            internet_message_id=internet_msg_id,
            conversation_id=conv_id,
            subject=subject,
            body=body,
            body_preview=body[:200] if body else "",
            from_address="user@example.com",
            from_name="Test User",
            to_recipients=to_recipients,
            cc_recipients=cc_recipients or [],
            sent_datetime=datetime.utcnow()
        )

        self.sent.append(msg)
        self.conversations[conv_id].append(msg)

        self.send_log.append({
            "type": "new_message",
            "message_id": msg_id,
            "to": to_recipients,
            "subject": subject,
            "body": body,
            "timestamp": datetime.utcnow().isoformat()
        })
        return True

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """Get attachments for a message."""
        for msg in self.inbox + self.sent:
            if msg.id == message_id:
                return msg.attachments
        return []

    def lookup_message_by_internet_id(self, internet_message_id: str) -> Optional[str]:
        """Lookup Graph message ID by internet message ID."""
        for msg in self.inbox + self.sent:
            if msg.internet_message_id == internet_message_id:
                return msg.id
        return None

    def get_conversation_thread(self, conversation_id: str) -> List[EmailMessage]:
        """Get all messages in a conversation."""
        return self.conversations.get(conversation_id, [])

    def clear(self):
        """Reset all data."""
        self.inbox.clear()
        self.sent.clear()
        self.drafts.clear()
        self.send_log.clear()
        self.reply_log.clear()
        self.conversations.clear()


# =============================================================================
# MOCK SHEETS PROVIDER
# =============================================================================

class MockSheetsProvider(SheetsProvider):
    """
    In-memory mock for Google Sheets operations.

    Features:
    - Stores sheet data as 2D arrays
    - Supports multiple sheets per spreadsheet
    - Tracks all write operations for verification
    """

    def __init__(self):
        # Structure: {sheet_id: {sheet_name: [[cell_values]]}}
        self.spreadsheets: Dict[str, Dict[str, List[List[Any]]]] = {}
        self.metadata: Dict[str, Dict] = {}

        # For tracking/verification
        self.write_log: List[Dict[str, Any]] = []
        self.batch_log: List[Dict[str, Any]] = []

    def create_spreadsheet(self, sheet_id: str, sheet_name: str = "Sheet1",
                           headers: List[str] = None, rows: List[List[Any]] = None):
        """Create a mock spreadsheet with optional initial data."""
        if sheet_id not in self.spreadsheets:
            self.spreadsheets[sheet_id] = {}
            self.metadata[sheet_id] = {
                "spreadsheetId": sheet_id,
                "sheets": []
            }

        data = []
        if headers:
            data.append(headers)
        if rows:
            data.extend(rows)

        self.spreadsheets[sheet_id][sheet_name] = data
        self.metadata[sheet_id]["sheets"].append({
            "properties": {
                "sheetId": len(self.metadata[sheet_id]["sheets"]),
                "title": sheet_name
            }
        })

    def _parse_range(self, range_notation: str) -> tuple:
        """Parse A1 notation into (sheet_name, start_col, start_row, end_col, end_row)."""
        # Handle "Sheet1!A1:B10" or just "A1:B10"
        if "!" in range_notation:
            sheet_name, cell_range = range_notation.split("!", 1)
        else:
            sheet_name = "Sheet1"
            cell_range = range_notation

        # Parse cell range
        def col_to_idx(col_str):
            result = 0
            for char in col_str.upper():
                result = result * 26 + (ord(char) - ord('A') + 1)
            return result - 1  # 0-indexed

        def parse_cell(cell):
            match = re.match(r"([A-Za-z]+)(\d+)", cell)
            if match:
                return col_to_idx(match.group(1)), int(match.group(2)) - 1
            return 0, 0

        if ":" in cell_range:
            start, end = cell_range.split(":")
            start_col, start_row = parse_cell(start)
            end_col, end_row = parse_cell(end)
        else:
            start_col, start_row = parse_cell(cell_range)
            end_col, end_row = start_col, start_row

        return sheet_name, start_col, start_row, end_col, end_row

    def get_values(self, sheet_id: str, range_notation: str) -> List[List[Any]]:
        """Get values from a range."""
        if sheet_id not in self.spreadsheets:
            return []

        sheet_name, start_col, start_row, end_col, end_row = self._parse_range(range_notation)

        if sheet_name not in self.spreadsheets[sheet_id]:
            return []

        data = self.spreadsheets[sheet_id][sheet_name]
        result = []

        for row_idx in range(start_row, min(end_row + 1, len(data))):
            if row_idx < len(data):
                row = data[row_idx]
                row_slice = row[start_col:end_col + 1] if row else []
                # Pad with empty strings if needed
                while len(row_slice) < (end_col - start_col + 1):
                    row_slice.append("")
                result.append(row_slice)

        return result

    def update_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        """Update values in a range."""
        sheet_name, start_col, start_row, end_col, end_row = self._parse_range(range_notation)

        # Ensure spreadsheet and sheet exist
        if sheet_id not in self.spreadsheets:
            self.spreadsheets[sheet_id] = {}
        if sheet_name not in self.spreadsheets[sheet_id]:
            self.spreadsheets[sheet_id][sheet_name] = []

        data = self.spreadsheets[sheet_id][sheet_name]

        # Expand data if needed
        while len(data) <= start_row + len(values) - 1:
            data.append([])

        # Write values
        for i, row_values in enumerate(values):
            row_idx = start_row + i
            while len(data[row_idx]) <= start_col + len(row_values) - 1:
                data[row_idx].append("")

            for j, value in enumerate(row_values):
                col_idx = start_col + j
                data[row_idx][col_idx] = value

        self.write_log.append({
            "sheet_id": sheet_id,
            "range": range_notation,
            "values": values,
            "timestamp": datetime.utcnow().isoformat()
        })

        return {"updatedRows": len(values), "updatedColumns": len(values[0]) if values else 0}

    def batch_update_values(self, sheet_id: str,
                            data: List[Dict[str, Any]]) -> Dict:
        """Batch update multiple ranges."""
        responses = []
        for item in data:
            resp = self.update_values(sheet_id, item["range"], item["values"])
            responses.append(resp)

        self.batch_log.append({
            "sheet_id": sheet_id,
            "data": data,
            "timestamp": datetime.utcnow().isoformat()
        })

        return {"responses": responses}

    def append_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        """Append values to a range."""
        sheet_name, start_col, start_row, end_col, end_row = self._parse_range(range_notation)

        if sheet_id not in self.spreadsheets:
            self.spreadsheets[sheet_id] = {}
        if sheet_name not in self.spreadsheets[sheet_id]:
            self.spreadsheets[sheet_id][sheet_name] = []

        data = self.spreadsheets[sheet_id][sheet_name]

        # Find next empty row
        next_row = len(data)

        for row_values in values:
            # Pad row if needed
            while len(row_values) < start_col:
                row_values.insert(0, "")
            data.append(row_values)

        self.write_log.append({
            "type": "append",
            "sheet_id": sheet_id,
            "range": range_notation,
            "values": values,
            "timestamp": datetime.utcnow().isoformat()
        })

        return {"updates": {"updatedRows": len(values)}}

    def get_sheet_metadata(self, sheet_id: str) -> Dict:
        """Get sheet metadata."""
        return self.metadata.get(sheet_id, {
            "spreadsheetId": sheet_id,
            "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
        })

    def get_cell(self, sheet_id: str, sheet_name: str, row: int, col: int) -> Any:
        """Helper to get a single cell value (0-indexed)."""
        if sheet_id not in self.spreadsheets:
            return ""
        if sheet_name not in self.spreadsheets[sheet_id]:
            return ""
        data = self.spreadsheets[sheet_id][sheet_name]
        if row >= len(data):
            return ""
        if col >= len(data[row]):
            return ""
        return data[row][col]

    def clear(self):
        """Reset all data."""
        self.spreadsheets.clear()
        self.metadata.clear()
        self.write_log.clear()
        self.batch_log.clear()


# =============================================================================
# MOCK FIRESTORE PROVIDER
# =============================================================================

class MockFirestoreProvider(FirestoreProvider):
    """
    In-memory mock for Firestore operations.

    Features:
    - Stores documents in nested dict structure
    - Supports full CRUD operations
    - Tracks all operations for verification
    """

    def __init__(self):
        # Structure: {collection: {doc_id: {data}}}
        self.data: Dict[str, Dict[str, Dict]] = defaultdict(dict)

        # For tracking/verification
        self.write_log: List[Dict[str, Any]] = []
        self.delete_log: List[Dict[str, Any]] = []

    def _parse_path(self, path: str) -> tuple:
        """Parse path into (collection_path, doc_id) or (collection_path, None)."""
        parts = path.split("/")
        if len(parts) % 2 == 0:
            # Path to document
            collection = "/".join(parts[:-1])
            doc_id = parts[-1]
            return collection, doc_id
        else:
            # Path to collection
            return path, None

    def get_document(self, path: str) -> FirestoreDocument:
        """Get a document by path."""
        collection, doc_id = self._parse_path(path)
        if doc_id and collection in self.data and doc_id in self.data[collection]:
            return FirestoreDocument(
                id=doc_id,
                data=self.data[collection][doc_id].copy(),
                exists=True
            )
        return FirestoreDocument(id=doc_id or "", data={}, exists=False)

    def set_document(self, path: str, data: Dict[str, Any], merge: bool = True) -> None:
        """Set/merge a document."""
        collection, doc_id = self._parse_path(path)
        if not doc_id:
            raise ValueError("Cannot set a collection, need document path")

        if merge and collection in self.data and doc_id in self.data[collection]:
            existing = self.data[collection][doc_id]
            existing.update(data)
        else:
            self.data[collection][doc_id] = data.copy()

        self.write_log.append({
            "operation": "set",
            "path": path,
            "data": data,
            "merge": merge,
            "timestamp": datetime.utcnow().isoformat()
        })

    def update_document(self, path: str, data: Dict[str, Any]) -> None:
        """Update specific fields in a document."""
        collection, doc_id = self._parse_path(path)
        if not doc_id:
            raise ValueError("Cannot update a collection, need document path")

        if collection not in self.data or doc_id not in self.data[collection]:
            raise ValueError(f"Document does not exist: {path}")

        self.data[collection][doc_id].update(data)

        self.write_log.append({
            "operation": "update",
            "path": path,
            "data": data,
            "timestamp": datetime.utcnow().isoformat()
        })

    def delete_document(self, path: str) -> None:
        """Delete a document."""
        collection, doc_id = self._parse_path(path)
        if not doc_id:
            raise ValueError("Cannot delete a collection, need document path")

        if collection in self.data and doc_id in self.data[collection]:
            del self.data[collection][doc_id]

        self.delete_log.append({
            "path": path,
            "timestamp": datetime.utcnow().isoformat()
        })

    def query_collection(self, path: str, filters: List[tuple] = None,
                         order_by: str = None, limit: int = None) -> List[FirestoreDocument]:
        """Query a collection with optional filters."""
        if path not in self.data:
            return []

        docs = []
        for doc_id, doc_data in self.data[path].items():
            # Apply filters
            matches = True
            if filters:
                for field, op, value in filters:
                    doc_value = doc_data.get(field)
                    if op == "==":
                        matches = matches and (doc_value == value)
                    elif op == ">":
                        matches = matches and (doc_value is not None and doc_value > value)
                    elif op == "<":
                        matches = matches and (doc_value is not None and doc_value < value)
                    elif op == ">=":
                        matches = matches and (doc_value is not None and doc_value >= value)
                    elif op == "<=":
                        matches = matches and (doc_value is not None and doc_value <= value)
                    elif op == "in":
                        matches = matches and (doc_value in value)

            if matches:
                docs.append(FirestoreDocument(id=doc_id, data=doc_data.copy(), exists=True))

        # Sort
        if order_by:
            reverse = order_by.startswith("-")
            field = order_by.lstrip("-")
            docs.sort(key=lambda d: d.data.get(field, ""), reverse=reverse)

        # Limit
        if limit:
            docs = docs[:limit]

        return docs

    def list_subcollection(self, path: str) -> List[FirestoreDocument]:
        """List all documents in a subcollection."""
        return self.query_collection(path)

    def clear(self):
        """Reset all data."""
        self.data.clear()
        self.write_log.clear()
        self.delete_log.clear()


# =============================================================================
# MOCK DRIVE PROVIDER
# =============================================================================

class MockDriveProvider(DriveProvider):
    """
    In-memory mock for Google Drive operations.

    Features:
    - Simulates file/folder storage
    - Generates fake web view links
    - Tracks all uploads for verification
    """

    def __init__(self):
        # Structure: {file_id: {name, content, mime_type, parent_id, webViewLink}}
        self.files: Dict[str, Dict[str, Any]] = {}

        # For tracking
        self.upload_log: List[Dict[str, Any]] = []

    def list_files(self, query: str, page_size: int = 10) -> List[Dict[str, Any]]:
        """List files matching a query."""
        results = []
        for file_id, file_data in self.files.items():
            # Simple query parsing
            if "name=" in query:
                match = re.search(r"name='([^']+)'", query)
                if match and match.group(1) != file_data["name"]:
                    continue
            if "mimeType=" in query:
                match = re.search(r"mimeType='([^']+)'", query)
                if match and match.group(1) != file_data.get("mime_type"):
                    continue
            if "parents" in query:
                match = re.search(r"'([^']+)' in parents", query)
                if match and match.group(1) != file_data.get("parent_id"):
                    continue

            results.append({
                "id": file_id,
                "name": file_data["name"],
                "webViewLink": file_data["webViewLink"]
            })

        return results[:page_size]

    def create_folder(self, name: str, parent_id: str = None) -> Dict[str, str]:
        """Create a folder."""
        folder_id = generate_id("folder_")
        self.files[folder_id] = {
            "name": name,
            "mime_type": "application/vnd.google-apps.folder",
            "parent_id": parent_id,
            "webViewLink": f"https://drive.google.com/drive/folders/{folder_id}"
        }
        return {"id": folder_id, "webViewLink": self.files[folder_id]["webViewLink"]}

    def upload_file(self, name: str, content: bytes, mime_type: str,
                    parent_id: str = None) -> Dict[str, str]:
        """Upload a file."""
        file_id = generate_id("file_")
        self.files[file_id] = {
            "name": name,
            "content": content,
            "mime_type": mime_type,
            "parent_id": parent_id,
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"
        }

        self.upload_log.append({
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size": len(content),
            "timestamp": datetime.utcnow().isoformat()
        })

        return {"id": file_id, "webViewLink": self.files[file_id]["webViewLink"]}

    def set_public_permission(self, file_id: str) -> bool:
        """Make file publicly readable."""
        if file_id in self.files:
            self.files[file_id]["public"] = True
            return True
        return False

    def clear(self):
        """Reset all data."""
        self.files.clear()
        self.upload_log.clear()


# =============================================================================
# MOCK OPENAI PROVIDER
# =============================================================================

class MockOpenAIProvider(OpenAIProvider):
    """
    Mock for OpenAI operations.

    Can be configured with predefined responses or use a response generator function.
    """

    def __init__(self, response_generator: Callable = None):
        self.response_generator = response_generator
        self.call_log: List[Dict[str, Any]] = []
        self.predefined_responses: Dict[str, str] = {}
        self.uploaded_files: Dict[str, bytes] = {}

    def set_response(self, pattern: str, response: str):
        """Set a predefined response for messages matching a pattern."""
        self.predefined_responses[pattern] = response

    def chat_completion(self, messages: List[Dict[str, str]],
                        model: str = None,
                        temperature: float = 0.3,
                        response_format: Dict = None) -> str:
        """Get a chat completion."""
        self.call_log.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "timestamp": datetime.utcnow().isoformat()
        })

        # Check predefined responses
        last_message = messages[-1]["content"] if messages else ""
        for pattern, response in self.predefined_responses.items():
            if pattern in last_message:
                return response

        # Use generator if provided
        if self.response_generator:
            return self.response_generator(messages, model, temperature, response_format)

        # Default mock response for email extraction
        return json.dumps({
            "updates": [],
            "events": [],
            "response_email": "Thank you for your response. I'll follow up with any additional questions.",
            "reasoning": "Mock AI response"
        })

    def upload_file(self, content: bytes, filename: str,
                    purpose: str = "user_data") -> str:
        """Upload a file."""
        file_id = generate_id("file-")
        self.uploaded_files[file_id] = content
        return file_id

    def clear(self):
        """Reset all data."""
        self.call_log.clear()
        self.predefined_responses.clear()
        self.uploaded_files.clear()


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

def create_mock_services(openai_generator: Callable = None) -> Dict[str, Any]:
    """
    Create a complete set of mock services for testing.

    Args:
        openai_generator: Optional function to generate AI responses.
                          Signature: (messages, model, temperature, response_format) -> str

    Returns:
        Dict mapping service names to mock providers
    """
    return {
        "email": MockEmailProvider(),
        "sheets": MockSheetsProvider(),
        "firestore": MockFirestoreProvider(),
        "drive": MockDriveProvider(),
        "openai": MockOpenAIProvider(response_generator=openai_generator)
    }


def reset_all_mocks(mocks: Dict[str, Any]):
    """Reset all mock services to initial state."""
    for mock in mocks.values():
        if hasattr(mock, 'clear'):
            mock.clear()
