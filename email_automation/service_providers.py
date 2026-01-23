"""
Service Providers Abstraction Layer

This module provides a unified interface for all external services used by the
email automation system. In production, it uses real API clients. In test mode,
it uses mock implementations that simulate the full pipeline without external calls.

Usage:
    from email_automation.service_providers import get_provider, set_test_mode

    # Production (default)
    email_provider = get_provider('email')
    email_provider.send_message(...)

    # Test mode
    set_test_mode(True, mock_services=my_mocks)
    email_provider = get_provider('email')  # Returns mock
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
import uuid


# =============================================================================
# CONFIGURATION
# =============================================================================

_test_mode = False
_mock_services: Optional[Dict[str, Any]] = None


def set_test_mode(enabled: bool, mock_services: Optional[Dict[str, Any]] = None):
    """Enable or disable test mode with optional mock service implementations."""
    global _test_mode, _mock_services
    _test_mode = enabled
    _mock_services = mock_services or {}


def is_test_mode() -> bool:
    """Check if test mode is enabled."""
    return _test_mode


def get_provider(service_name: str):
    """Get the appropriate provider for a service (real or mock)."""
    if _test_mode and _mock_services and service_name in _mock_services:
        return _mock_services[service_name]

    # Return real provider
    if service_name == 'email':
        return RealEmailProvider()
    elif service_name == 'sheets':
        return RealSheetsProvider()
    elif service_name == 'firestore':
        return RealFirestoreProvider()
    elif service_name == 'drive':
        return RealDriveProvider()
    elif service_name == 'openai':
        return RealOpenAIProvider()
    else:
        raise ValueError(f"Unknown service: {service_name}")


# =============================================================================
# EMAIL PROVIDER (Microsoft Graph)
# =============================================================================

@dataclass
class EmailMessage:
    """Represents an email message."""
    id: str
    internet_message_id: str
    conversation_id: str
    subject: str
    body: str
    body_preview: str
    from_address: str
    from_name: str
    to_recipients: List[str]
    cc_recipients: List[str] = field(default_factory=list)
    received_datetime: Optional[datetime] = None
    sent_datetime: Optional[datetime] = None
    has_attachments: bool = False
    attachments: List[Dict[str, Any]] = field(default_factory=list)


class EmailProvider(ABC):
    """Abstract interface for email operations."""

    @abstractmethod
    def list_messages(self, folder: str = "inbox", filter_query: str = None,
                      top: int = 50) -> List[EmailMessage]:
        """List messages from a folder with optional filtering."""
        pass

    @abstractmethod
    def get_message(self, message_id: str) -> EmailMessage:
        """Get a single message by ID with full body."""
        pass

    @abstractmethod
    def create_draft(self, subject: str, body: str, to_recipients: List[str],
                     cc_recipients: List[str] = None,
                     headers: Dict[str, str] = None) -> Dict[str, str]:
        """Create a draft message. Returns {id, internetMessageId, conversationId}."""
        pass

    @abstractmethod
    def send_draft(self, draft_id: str) -> bool:
        """Send a draft message. Returns True on success."""
        pass

    @abstractmethod
    def reply_to_message(self, message_id: str, body: str) -> bool:
        """Reply to a message in-thread. Returns True on success."""
        pass

    @abstractmethod
    def send_new_message(self, subject: str, body: str, to_recipients: List[str],
                         cc_recipients: List[str] = None) -> bool:
        """Send a new message (not in-thread). Returns True on success."""
        pass

    @abstractmethod
    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """Get attachments for a message."""
        pass

    @abstractmethod
    def lookup_message_by_internet_id(self, internet_message_id: str) -> Optional[str]:
        """Lookup Graph message ID by internet message ID."""
        pass


class RealEmailProvider(EmailProvider):
    """Real implementation using Microsoft Graph API."""

    def __init__(self):
        # Import here to avoid circular imports
        from email_automation.clients import get_graph_token
        self.get_token = get_graph_token

    def _get_headers(self):
        token = self.get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def list_messages(self, folder: str = "inbox", filter_query: str = None,
                      top: int = 50) -> List[EmailMessage]:
        import requests
        headers = self._get_headers()
        url = f"https://graph.microsoft.com/v1.0/me/messages"
        params = {
            "$select": "id,internetMessageId,conversationId,subject,bodyPreview,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments",
            "$orderby": "receivedDateTime desc",
            "$top": top
        }
        if filter_query:
            params["$filter"] = filter_query

        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()

        messages = []
        for msg in resp.json().get("value", []):
            messages.append(EmailMessage(
                id=msg["id"],
                internet_message_id=msg.get("internetMessageId", ""),
                conversation_id=msg.get("conversationId", ""),
                subject=msg.get("subject", ""),
                body="",  # Not fetched in list
                body_preview=msg.get("bodyPreview", ""),
                from_address=msg.get("from", {}).get("emailAddress", {}).get("address", ""),
                from_name=msg.get("from", {}).get("emailAddress", {}).get("name", ""),
                to_recipients=[r["emailAddress"]["address"] for r in msg.get("toRecipients", [])],
                cc_recipients=[r["emailAddress"]["address"] for r in msg.get("ccRecipients", [])],
                received_datetime=msg.get("receivedDateTime"),
                has_attachments=msg.get("hasAttachments", False)
            ))
        return messages

    def get_message(self, message_id: str) -> EmailMessage:
        import requests
        headers = self._get_headers()
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        params = {
            "$select": "id,internetMessageId,conversationId,subject,body,bodyPreview,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments"
        }
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        msg = resp.json()

        return EmailMessage(
            id=msg["id"],
            internet_message_id=msg.get("internetMessageId", ""),
            conversation_id=msg.get("conversationId", ""),
            subject=msg.get("subject", ""),
            body=msg.get("body", {}).get("content", ""),
            body_preview=msg.get("bodyPreview", ""),
            from_address=msg.get("from", {}).get("emailAddress", {}).get("address", ""),
            from_name=msg.get("from", {}).get("emailAddress", {}).get("name", ""),
            to_recipients=[r["emailAddress"]["address"] for r in msg.get("toRecipients", [])],
            cc_recipients=[r["emailAddress"]["address"] for r in msg.get("ccRecipients", [])],
            received_datetime=msg.get("receivedDateTime"),
            has_attachments=msg.get("hasAttachments", False)
        )

    def create_draft(self, subject: str, body: str, to_recipients: List[str],
                     cc_recipients: List[str] = None,
                     headers_extra: Dict[str, str] = None) -> Dict[str, str]:
        import requests
        headers = self._get_headers()
        url = "https://graph.microsoft.com/v1.0/me/messages"

        payload = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": r}} for r in to_recipients]
        }
        if cc_recipients:
            payload["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]

        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        draft = resp.json()

        # Fetch metadata
        meta_resp = requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages/{draft['id']}",
            headers=headers,
            params={"$select": "internetMessageId,conversationId"}
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        return {
            "id": draft["id"],
            "internetMessageId": meta.get("internetMessageId", ""),
            "conversationId": meta.get("conversationId", "")
        }

    def send_draft(self, draft_id: str) -> bool:
        import requests
        headers = self._get_headers()
        url = f"https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send"
        resp = requests.post(url, headers=headers)
        return resp.status_code == 202

    def reply_to_message(self, message_id: str, body: str) -> bool:
        import requests
        headers = self._get_headers()
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/reply"
        payload = {"message": {"body": {"contentType": "HTML", "content": body}}}
        resp = requests.post(url, headers=headers, json=payload)
        return resp.status_code in [200, 201, 202]

    def send_new_message(self, subject: str, body: str, to_recipients: List[str],
                         cc_recipients: List[str] = None) -> bool:
        import requests
        headers = self._get_headers()
        url = "https://graph.microsoft.com/v1.0/me/sendMail"
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [{"emailAddress": {"address": r}} for r in to_recipients]
            },
            "saveToSentItems": True
        }
        if cc_recipients:
            payload["message"]["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]

        resp = requests.post(url, headers=headers, json=payload)
        return resp.status_code in [200, 201, 202]

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        import requests
        headers = self._get_headers()
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def lookup_message_by_internet_id(self, internet_message_id: str) -> Optional[str]:
        import requests
        headers = self._get_headers()
        url = "https://graph.microsoft.com/v1.0/me/messages"
        params = {
            "$filter": f"internetMessageId eq '{internet_message_id}'",
            "$select": "id"
        }
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            value = resp.json().get("value", [])
            if value:
                return value[0]["id"]
        return None


# =============================================================================
# SHEETS PROVIDER (Google Sheets)
# =============================================================================

class SheetsProvider(ABC):
    """Abstract interface for Google Sheets operations."""

    @abstractmethod
    def get_values(self, sheet_id: str, range_notation: str) -> List[List[Any]]:
        """Get values from a range. Returns 2D list."""
        pass

    @abstractmethod
    def update_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        """Update values in a range."""
        pass

    @abstractmethod
    def batch_update_values(self, sheet_id: str,
                            data: List[Dict[str, Any]]) -> Dict:
        """Batch update multiple ranges."""
        pass

    @abstractmethod
    def append_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        """Append values to a range."""
        pass

    @abstractmethod
    def get_sheet_metadata(self, sheet_id: str) -> Dict:
        """Get sheet metadata (sheet names, IDs, etc.)."""
        pass


class RealSheetsProvider(SheetsProvider):
    """Real implementation using Google Sheets API."""

    def __init__(self):
        from email_automation.clients import sheets_service
        self.sheets = sheets_service()

    def get_values(self, sheet_id: str, range_notation: str) -> List[List[Any]]:
        result = self.sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_notation
        ).execute()
        return result.get("values", [])

    def update_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        return self.sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_notation,
            valueInputOption=value_input_option,
            body={"values": values}
        ).execute()

    def batch_update_values(self, sheet_id: str,
                            data: List[Dict[str, Any]]) -> Dict:
        return self.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data}
        ).execute()

    def append_values(self, sheet_id: str, range_notation: str,
                      values: List[List[Any]], value_input_option: str = "RAW") -> Dict:
        return self.sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=range_notation,
            valueInputOption=value_input_option,
            body={"values": values}
        ).execute()

    def get_sheet_metadata(self, sheet_id: str) -> Dict:
        return self.sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()


# =============================================================================
# FIRESTORE PROVIDER
# =============================================================================

@dataclass
class FirestoreDocument:
    """Represents a Firestore document."""
    id: str
    data: Dict[str, Any]
    exists: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return self.data


class FirestoreProvider(ABC):
    """Abstract interface for Firestore operations."""

    @abstractmethod
    def get_document(self, path: str) -> FirestoreDocument:
        """Get a document by path (e.g., 'users/uid/clients/clientId')."""
        pass

    @abstractmethod
    def set_document(self, path: str, data: Dict[str, Any], merge: bool = True) -> None:
        """Set/merge a document."""
        pass

    @abstractmethod
    def update_document(self, path: str, data: Dict[str, Any]) -> None:
        """Update specific fields in a document."""
        pass

    @abstractmethod
    def delete_document(self, path: str) -> None:
        """Delete a document."""
        pass

    @abstractmethod
    def query_collection(self, path: str, filters: List[tuple] = None,
                         order_by: str = None, limit: int = None) -> List[FirestoreDocument]:
        """Query a collection with optional filters."""
        pass

    @abstractmethod
    def list_subcollection(self, path: str) -> List[FirestoreDocument]:
        """List all documents in a subcollection."""
        pass


class RealFirestoreProvider(FirestoreProvider):
    """Real implementation using Google Cloud Firestore."""

    def __init__(self):
        from email_automation.clients import firestore_client
        self.db = firestore_client()

    def _path_to_ref(self, path: str):
        """Convert path string to document/collection reference."""
        parts = path.split("/")
        ref = self.db
        for i, part in enumerate(parts):
            if i % 2 == 0:
                ref = ref.collection(part)
            else:
                ref = ref.document(part)
        return ref

    def get_document(self, path: str) -> FirestoreDocument:
        ref = self._path_to_ref(path)
        snap = ref.get()
        return FirestoreDocument(
            id=snap.id if hasattr(snap, 'id') else path.split("/")[-1],
            data=snap.to_dict() or {},
            exists=snap.exists
        )

    def set_document(self, path: str, data: Dict[str, Any], merge: bool = True) -> None:
        ref = self._path_to_ref(path)
        ref.set(data, merge=merge)

    def update_document(self, path: str, data: Dict[str, Any]) -> None:
        ref = self._path_to_ref(path)
        ref.update(data)

    def delete_document(self, path: str) -> None:
        ref = self._path_to_ref(path)
        ref.delete()

    def query_collection(self, path: str, filters: List[tuple] = None,
                         order_by: str = None, limit: int = None) -> List[FirestoreDocument]:
        ref = self._path_to_ref(path)
        query = ref

        if filters:
            for field, op, value in filters:
                query = query.where(field, op, value)
        if order_by:
            query = query.order_by(order_by)
        if limit:
            query = query.limit(limit)

        docs = []
        for snap in query.stream():
            docs.append(FirestoreDocument(
                id=snap.id,
                data=snap.to_dict() or {},
                exists=True
            ))
        return docs

    def list_subcollection(self, path: str) -> List[FirestoreDocument]:
        ref = self._path_to_ref(path)
        docs = []
        for snap in ref.stream():
            docs.append(FirestoreDocument(
                id=snap.id,
                data=snap.to_dict() or {},
                exists=True
            ))
        return docs


# =============================================================================
# DRIVE PROVIDER (Google Drive)
# =============================================================================

class DriveProvider(ABC):
    """Abstract interface for Google Drive operations."""

    @abstractmethod
    def list_files(self, query: str, page_size: int = 10) -> List[Dict[str, Any]]:
        """List files matching a query."""
        pass

    @abstractmethod
    def create_folder(self, name: str, parent_id: str = None) -> Dict[str, str]:
        """Create a folder. Returns {id, webViewLink}."""
        pass

    @abstractmethod
    def upload_file(self, name: str, content: bytes, mime_type: str,
                    parent_id: str = None) -> Dict[str, str]:
        """Upload a file. Returns {id, webViewLink}."""
        pass

    @abstractmethod
    def set_public_permission(self, file_id: str) -> bool:
        """Make file publicly readable."""
        pass


class RealDriveProvider(DriveProvider):
    """Real implementation using Google Drive API."""

    def __init__(self):
        from email_automation.clients import drive_service
        self.drive = drive_service()

    def list_files(self, query: str, page_size: int = 10) -> List[Dict[str, Any]]:
        result = self.drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name, webViewLink)",
            pageSize=page_size
        ).execute()
        return result.get("files", [])

    def create_folder(self, name: str, parent_id: str = None) -> Dict[str, str]:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        folder = self.drive.files().create(
            body=metadata,
            fields="id,webViewLink"
        ).execute()
        return {"id": folder["id"], "webViewLink": folder.get("webViewLink", "")}

    def upload_file(self, name: str, content: bytes, mime_type: str,
                    parent_id: str = None) -> Dict[str, str]:
        import io
        from googleapiclient.http import MediaIoBaseUpload

        metadata = {"name": name}
        if parent_id:
            metadata["parents"] = [parent_id]

        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type)
        file = self.drive.files().create(
            body=metadata,
            media_body=media,
            fields="id,webViewLink"
        ).execute()
        return {"id": file["id"], "webViewLink": file.get("webViewLink", "")}

    def set_public_permission(self, file_id: str) -> bool:
        try:
            self.drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"}
            ).execute()
            return True
        except Exception:
            return False


# =============================================================================
# OPENAI PROVIDER
# =============================================================================

class OpenAIProvider(ABC):
    """Abstract interface for OpenAI operations."""

    @abstractmethod
    def chat_completion(self, messages: List[Dict[str, str]],
                        model: str = None,
                        temperature: float = 0.3,
                        response_format: Dict = None) -> str:
        """Get a chat completion. Returns the assistant's response content."""
        pass

    @abstractmethod
    def upload_file(self, content: bytes, filename: str,
                    purpose: str = "user_data") -> str:
        """Upload a file. Returns file ID."""
        pass


class RealOpenAIProvider(OpenAIProvider):
    """Real implementation using OpenAI API."""

    def __init__(self):
        from email_automation.clients import openai_client
        self.client = openai_client()
        import os
        self.model = os.environ.get("OPENAI_ASSISTANT_MODEL", "gpt-4o")

    def chat_completion(self, messages: List[Dict[str, str]],
                        model: str = None,
                        temperature: float = 0.3,
                        response_format: Dict = None) -> str:
        kwargs = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature
        }
        if response_format:
            kwargs["response_format"] = response_format

        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def upload_file(self, content: bytes, filename: str,
                    purpose: str = "user_data") -> str:
        import io
        file_obj = io.BytesIO(content)
        file_obj.name = filename
        response = self.client.files.create(file=file_obj, purpose=purpose)
        return response.id


# =============================================================================
# HELPER: Generate test IDs
# =============================================================================

def generate_id(prefix: str = "") -> str:
    """Generate a unique ID for testing."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def generate_message_id() -> str:
    """Generate a realistic internet message ID."""
    return f"<{uuid.uuid4().hex}@test.example.com>"


def generate_conversation_id() -> str:
    """Generate a conversation ID."""
    return f"AAQk{uuid.uuid4().hex[:20]}"
