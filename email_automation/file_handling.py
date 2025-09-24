import os
import base64
import requests
import tempfile
from typing import List, Dict, Any
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
from .clients import _helper_google_creds, client

def fetch_pdf_attachments(headers: Dict[str, str], graph_msg_id: str) -> List[Dict[str, Any]]:
    """Fetch PDF attachments from current message only."""
    try:
        base = "https://graph.microsoft.com/v1.0"
        
        # Get attachments
        resp = requests.get(
            f"{base}/me/messages/{graph_msg_id}/attachments",
            headers=headers,
            timeout=30
        )
        resp.raise_for_status()
        
        attachments = resp.json().get("value", [])
        pdf_attachments = []
        
        for attachment in attachments:
            if attachment.get("contentType", "").lower() == "application/pdf":
                name = attachment.get("name", "document.pdf")
                content_bytes = base64.b64decode(attachment.get("contentBytes", ""))
                pdf_attachments.append({
                    "name": name,
                    "bytes": content_bytes
                })
        
        print(f"📎 Found {len(pdf_attachments)} PDF attachment(s)")
        return pdf_attachments
        
    except Exception as e:
        print(f"❌ Failed to fetch PDF attachments: {e}")
        return []

def ensure_drive_folder():
    """Ensure Drive folder exists and return folder ID."""
    try:
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        # Search for existing folder
        results = drive.files().list(
            q="name='Email PDFs' and mimeType='application/vnd.google-apps.folder'",
            spaces="drive"
        ).execute()
        
        folders = results.get("files", [])
        if folders:
            return folders[0]["id"]
        
        # Create folder
        folder_metadata = {
            "name": "Email PDFs",
            "mimeType": "application/vnd.google-apps.folder"
        }
        
        folder = drive.files().create(body=folder_metadata).execute()
        print(f"📁 Created Drive folder: {folder.get('id')}")
        return folder.get("id")
        
    except Exception as e:
        print(f"❌ Failed to ensure Drive folder: {e}")
        return None

def upload_pdf_to_drive(name: str, content: bytes, folder_id: str = None) -> str | None:
    """Upload PDF to Drive and return webViewLink."""
    try:
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        if not folder_id:
            folder_id = ensure_drive_folder()
        
        file_metadata = {
            "name": name,
            "parents": [folder_id] if folder_id else []
        }
        
        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="application/pdf",
            resumable=True
        )
        
        file = drive.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,webViewLink"
        ).execute()
        
        # Make link-shareable
        drive.permissions().create(
            fileId=file.get("id"),
            body={
                "role": "reader",
                "type": "anyone"
            }
        ).execute()
        
        web_link = file.get("webViewLink")
        print(f"📁 Uploaded to Drive: {name} -> {web_link}")
        return web_link
        
    except Exception as e:
        print(f"❌ Failed to upload PDF to Drive: {e}")
        return None

def upload_pdf_user_data(filename: str, content: bytes) -> str:
    """Upload PDF to OpenAI with purpose='user_data' and return file_id."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            
            with open(tmp_file.name, "rb") as f:
                file_response = client.files.create(
                    file=f,
                    purpose="user_data"
                )
            
            os.unlink(tmp_file.name)  # Clean up
            
            file_id = file_response.id
            print(f"📤 Uploaded to OpenAI: {filename} -> {file_id}")
            return file_id
            
    except Exception as e:
        print(f"❌ Failed to upload PDF to OpenAI: {e}")
        raise