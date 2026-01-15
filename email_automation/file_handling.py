import os
import base64
import requests
import tempfile
from typing import List, Dict, Any, Tuple
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
from .clients import _helper_google_creds, client

# PDF extraction libraries
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    print("⚠️ pdfplumber not installed - PDF text extraction limited")

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    print("⚠️ PyMuPDF not installed - PDF image extraction limited")

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

def extract_pdf_text(content: bytes, filename: str = "document.pdf") -> Tuple[str, List[bytes]]:
    """
    Extract text from PDF using multiple strategies for maximum coverage.

    Returns:
        Tuple of (extracted_text, list_of_page_images_as_bytes)
        - extracted_text: All text found in the PDF
        - page_images: Images of pages with little/no text (for OCR fallback)
    """
    text_parts = []
    page_images = []

    # Track which pages have sufficient text (threshold: 50 chars per page)
    MIN_TEXT_PER_PAGE = 50

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Strategy 1: pdfplumber for text and tables (best for native PDFs)
        if HAS_PDFPLUMBER:
            try:
                with pdfplumber.open(tmp_path) as pdf:
                    for page_num, page in enumerate(pdf.pages):
                        page_text = ""

                        # Extract regular text
                        raw_text = page.extract_text() or ""
                        page_text += raw_text

                        # Extract tables (common in property brochures)
                        tables = page.extract_tables()
                        for table in tables:
                            for row in table:
                                if row:
                                    row_text = " | ".join([str(cell) if cell else "" for cell in row])
                                    page_text += "\n" + row_text

                        text_parts.append(f"--- Page {page_num + 1} ---\n{page_text.strip()}")

                        # If page has little text, mark for image extraction
                        if len(page_text.strip()) < MIN_TEXT_PER_PAGE:
                            print(f"  📄 Page {page_num + 1}: Low text ({len(page_text.strip())} chars) - will extract image")

                print(f"📄 pdfplumber extracted {sum(len(p) for p in text_parts)} chars from {filename}")
            except Exception as e:
                print(f"⚠️ pdfplumber failed for {filename}: {e}")

        # Strategy 2: PyMuPDF as fallback for text + image extraction for sparse pages
        if HAS_PYMUPDF:
            try:
                doc = fitz.open(tmp_path)
                pymupdf_text_parts = []

                for page_num in range(len(doc)):
                    page = doc[page_num]
                    page_text = page.get_text("text") or ""

                    # If pdfplumber didn't get much, add PyMuPDF text
                    if page_num < len(text_parts) and len(text_parts[page_num]) < MIN_TEXT_PER_PAGE + 30:
                        # Append PyMuPDF text if it has more
                        if len(page_text.strip()) > len(text_parts[page_num]):
                            text_parts[page_num] = f"--- Page {page_num + 1} ---\n{page_text.strip()}"
                    elif page_num >= len(text_parts):
                        pymupdf_text_parts.append(f"--- Page {page_num + 1} ---\n{page_text.strip()}")

                    # Convert pages with little text to images for vision API
                    combined_text = text_parts[page_num] if page_num < len(text_parts) else ""
                    if len(combined_text.strip().replace(f"--- Page {page_num + 1} ---", "").strip()) < MIN_TEXT_PER_PAGE:
                        if HAS_PILLOW:
                            # Render page to image at good resolution (150 DPI)
                            mat = fitz.Matrix(150/72, 150/72)
                            pix = page.get_pixmap(matrix=mat)
                            img_bytes = pix.tobytes("png")
                            page_images.append(img_bytes)
                            print(f"  🖼️ Converted page {page_num + 1} to image for vision analysis")

                text_parts.extend(pymupdf_text_parts)
                doc.close()

            except Exception as e:
                print(f"⚠️ PyMuPDF failed for {filename}: {e}")

        # Combine all extracted text
        full_text = "\n\n".join(text_parts)

        # Clean up text
        full_text = clean_extracted_text(full_text)

        print(f"✅ PDF extraction complete: {len(full_text)} chars text, {len(page_images)} page images")
        return full_text, page_images

    finally:
        os.unlink(tmp_path)


def clean_extracted_text(text: str) -> str:
    """Clean up extracted PDF text for better model comprehension."""
    import re

    # Remove excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)

    # Remove common PDF artifacts
    text = re.sub(r'\x00', '', text)  # Null bytes
    text = re.sub(r'[\x01-\x08\x0b\x0c\x0e-\x1f]', '', text)  # Control chars

    # Fix common OCR/extraction issues
    text = text.replace('|', ' | ')  # Space around pipe for tables
    text = re.sub(r'\s+\|', ' |', text)
    text = re.sub(r'\|\s+', '| ', text)

    return text.strip()


def process_pdf_for_ai(content: bytes, filename: str = "document.pdf") -> Dict[str, Any]:
    """
    Process a PDF and prepare it for AI consumption.

    Returns dict with:
        - 'text': Extracted text content
        - 'images': List of base64-encoded page images (for pages with little text)
        - 'method': How the content was extracted
        - 'file_id': OpenAI file ID if uploaded (fallback)
    """
    result = {
        'text': '',
        'images': [],
        'method': 'none',
        'file_id': None,
        'filename': filename
    }

    # Try local extraction first
    extracted_text, page_images = extract_pdf_text(content, filename)

    if extracted_text and len(extracted_text) > 100:
        result['text'] = extracted_text
        result['method'] = 'local_extraction'

        # Add images for pages with little text (for vision fallback)
        if page_images:
            result['images'] = [base64.b64encode(img).decode('utf-8') for img in page_images[:5]]  # Max 5 pages
            result['method'] = 'local_extraction+images'

        print(f"📄 PDF processed via local extraction: {len(extracted_text)} chars, {len(result['images'])} images")
        return result

    # Fallback: Upload to OpenAI if local extraction failed
    print(f"⚠️ Local extraction yielded little text, uploading to OpenAI...")
    try:
        file_id = upload_pdf_user_data(filename, content)
        result['file_id'] = file_id
        result['method'] = 'openai_upload'

        # Still include images if we have them
        if page_images:
            result['images'] = [base64.b64encode(img).decode('utf-8') for img in page_images[:5]]
            result['method'] = 'openai_upload+images'

        print(f"📄 PDF uploaded to OpenAI: {file_id}")
    except Exception as e:
        print(f"❌ Failed to upload PDF to OpenAI: {e}")
        result['method'] = 'failed'

    return result


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


def fetch_and_process_pdfs(headers: Dict[str, str], graph_msg_id: str) -> List[Dict[str, Any]]:
    """
    Fetch PDF attachments and process them for AI consumption.

    Returns list of processed PDFs with:
        - name: Original filename
        - text: Extracted text (if available)
        - images: Base64 encoded page images (for vision)
        - id: OpenAI file ID (fallback if local extraction failed)
        - method: How content was extracted
    """
    attachments = fetch_pdf_attachments(headers, graph_msg_id)

    processed = []
    for attachment in attachments:
        name = attachment.get("name", "document.pdf")
        content = attachment.get("bytes", b"")

        if not content:
            print(f"⚠️ Empty PDF attachment: {name}")
            continue

        print(f"\n📎 Processing PDF: {name} ({len(content)} bytes)")
        result = process_pdf_for_ai(content, name)
        result['name'] = name

        # Upload to Drive for archival
        try:
            drive_link = upload_pdf_to_drive(name, content)
            result['drive_link'] = drive_link
        except Exception as e:
            print(f"⚠️ Drive upload failed: {e}")
            result['drive_link'] = None

        processed.append(result)

    return processed