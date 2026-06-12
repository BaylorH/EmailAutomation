import re
import base64
import io
import time
import requests
import os
import logging
from bs4 import BeautifulSoup
from typing import Optional, List

logger = logging.getLogger(__name__)
SIGNATURE_INLINE_IMAGE_MAX_BYTES = 48 * 1024
SIGNATURE_INLINE_IMAGE_MAX_DIMENSION = 240

# Helper: detect HTML vs text
_html_rx = re.compile(r"<[a-zA-Z/][^>]*>")
_GLUED_DOCUMENT_SIGNOFF_RX = re.compile(
    r"(?i)(\.(?:pdf|docx?|xlsx?|pptx?|csv|zip))"
    r"(?:thank(?:s|you|\s+you)?|regards|best|sincerely|cheers|sent|from|on)"
    r"[\w,.;:!?\- ]*$"
)
_OUTBOUND_TEXT_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u2022": "-",
    "\u2026": "...",
    "\u00a0": " ",
})

def _body_kind(script: str):
    if script and _html_rx.search(script):
        return "HTML", script
    return "Text", script or ""

def _normalize_email(s: str) -> str:
    return (s or "").strip().lower()


def normalize_outbound_message_text(text: str) -> str:
    """Normalize generated copy before handing HTML to mail transports."""
    return (text or "").translate(_OUTBOUND_TEXT_TRANSLATION)


def strip_email_quotes(text: str) -> str:
    """
    Strip quoted content from email replies to get just the new message content.

    Handles common quote patterns:
    - "On [date] [name] wrote:" followed by quoted text
    - Lines starting with ">"
    - "-------- Original Message --------"
    - "From: ... Sent: ... To: ..." headers
    - Gmail-style "On ... wrote:" blocks

    Returns the text before any quoted content.
    If no new content exists (e.g., reply is just a PDF attachment with quoted original),
    returns "[No text content - see attachments]" to signal the AI to focus on PDFs.
    """
    if not text:
        return ""

    lines = text.split('\n')
    result_lines = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Stop at common quote indicators
        # Gmail/most clients: "On [date] [name] wrote:"
        if re.match(r'^On\s+.+wrote:\s*$', stripped, re.IGNORECASE):
            break

        # Outlook: "From: ... Sent: ... To: ..."
        if stripped.startswith('From:') and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line.startswith('Sent:') or next_line.startswith('To:'):
                break

        # Common dividers
        if stripped in ['-------- Original Message --------',
                        '________________________________',
                        '-----Original Message-----']:
            break

        # Lines that are just ">" or start with "> " are quoted
        if stripped == '>' or stripped.startswith('> '):
            # Skip quoted lines but continue looking for more content after
            continue

        result_lines.append(line)

    # Clean up result
    result = '\n'.join(result_lines).strip()

    # If we stripped everything (reply was just quoted content + attachments),
    # return a signal for the AI to focus on attachments
    if not result:
        return "[No new text content in reply - check PDF attachments for property information]"

    return result


# Email validation regex - RFC 5322 simplified
_EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
)
_RESERVED_EMAIL_TLDS = {"invalid", "test", "example", "localhost"}

def is_valid_email(email: str) -> bool:
    """
    Validate email address format.
    Returns True if email is well-formed, False otherwise.
    """
    if not email:
        return False
    email = email.strip()
    if not _EMAIL_REGEX.match(email):
        return False
    domain = email.rsplit('@', 1)[1].lower()
    labels = domain.split('.')
    if labels[-1] in _RESERVED_EMAIL_TLDS:
        return False
    if domain == "localhost":
        return False
    # Additional safety checks
    if len(email) > 254:  # Max email length per RFC 5321
        return False
    local_part = email.split('@')[0]
    if len(local_part) > 64:  # Max local part length
        return False
    return True

def validate_recipient_emails(emails: List[str]) -> tuple[List[str], List[str]]:
    """
    Validate a list of email addresses.
    Returns (valid_emails, invalid_emails).
    """
    valid = []
    invalid = []
    for email in emails:
        if is_valid_email(email):
            valid.append(email.strip().lower())
        else:
            invalid.append(email)
    return valid, invalid

def _norm_txt(x: str) -> str:
    return (x or "").strip().lower()

def b64url_id(message_id: str) -> str:
    """Encode message ID for safe use as Firestore document key."""
    encoded = base64.urlsafe_b64encode(message_id.encode('utf-8')).decode('ascii').rstrip('=')
    logger.debug(
        "message_id.b64url",
        extra={
            "input": message_id,
            "encoded": encoded,
        },
    )
    return encoded

def normalize_message_id(msg_id: str) -> str:
    """Normalize message ID - strip whitespace and angle brackets."""
    if not msg_id:
        return ""
    # Remove angle brackets if present (email headers often wrap IDs in < >)
    normalized = msg_id.strip().strip('<>')
    logger.debug(
        "message_id.normalize",
        extra={
            "input": msg_id,
            "normalized": normalized,
        },
    )
    return normalized

def parse_references_header(references: str) -> List[str]:
    """Parse References header into list of message IDs."""
    if not references:
        return []
    
    # Split by whitespace and filter non-empty tokens
    tokens = [token.strip() for token in references.split() if token.strip()]
    return tokens

def strip_html_tags(html: str) -> str:
    """Strip HTML tags for preview."""
    if not html:
        return ""
    # Simple HTML tag removal
    clean = re.sub(r'<[^>]+>', '', html)
    # Decode common HTML entities
    clean = clean.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    clean = clean.replace('&quot;', '"').replace('&#39;', "'")
    clean = clean.replace('&nbsp;', ' ')  # Non-breaking spaces
    # Collapse multiple spaces into single space
    clean = re.sub(r' +', ' ', clean)
    return clean.strip()


def clean_email_content(content: str) -> str:
    """
    Clean email content for AI processing.
    Handles HTML entities, excessive whitespace, and other formatting issues.
    """
    if not content:
        return ""

    # Replace HTML entities
    clean = content.replace('&nbsp;', ' ')
    clean = clean.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    clean = clean.replace('&quot;', '"').replace('&#39;', "'")

    # Collapse multiple spaces into single space (but preserve newlines)
    clean = re.sub(r'[ \t]+', ' ', clean)

    # Collapse multiple newlines into max 2
    clean = re.sub(r'\n{3,}', '\n\n', clean)

    return clean.strip()

def safe_preview(content: str, max_len: int = 200) -> str:
    """Create safe preview of email content."""
    preview = strip_html_tags(content) if content else ""
    if len(preview) > max_len:
        preview = preview[:max_len] + "..."
    return preview

def exponential_backoff_request(func, max_retries: int = 3):
    """Execute request with exponential backoff on rate limits."""
    for attempt in range(max_retries):
        try:
            response = func()
            if response.status_code == 429:  # Rate limited
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                print(f"⏳ Rate limited, retrying after {retry_after}s")
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response.status_code >= 500 and attempt < max_retries - 1:
                sleep_time = 2 ** attempt
                print(f"⏳ Server error, retrying after {sleep_time}s")
                time.sleep(sleep_time)
                continue
            raise
        except Exception as e:
            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt
                print(f"⏳ Request failed, retrying after {sleep_time}s")
                time.sleep(sleep_time)
                continue
            raise
    raise Exception(f"Request failed after {max_retries} attempts")

def fetch_url_as_text(url: str) -> Optional[str]:
    """
    Try to fetch URL content and extract visible text using BeautifulSoup.
    Returns None on any failure (fail-safe).
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        response.raise_for_status()
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Get text
        text = soup.get_text()
        
        # Clean up whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        
        # Limit size
        if len(text) > 5000:
            text = text[:5000] + "..."
        
        print(f"🌐 Fetched {len(text)} chars from {url}")
        return text
        
    except Exception as e:
        print(f"⚠️ Failed to fetch URL {url}: {e}")
        return None

def _sanitize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    # Trim common trailing junk (punctuation, stray words glued to the URL)
    u = re.sub(r'[\)\]\}\.,;:!?]+$', '', u)
    glued_signoff = _GLUED_DOCUMENT_SIGNOFF_RX.search(u)
    if glued_signoff:
        u = u[:glued_signoff.end(1)]
    # If a trailing capitalized token got glued on (e.g., 'Thank'/'Thanks'), drop it
    u = re.sub(r'(?i)(thank(?:s| you)?)$', '', u)
    u = re.sub(r'On$', '', u)
    return u

def _subject_to_address_city(subject: str) -> tuple[str, str]:
    if not subject:
        return "", ""
    s = re.sub(r'^(re:|fwd:)\s*', '', subject, flags=re.I).strip()
    s = re.sub(r'\s+\[.*?\]$', '', s)  # drop trailing bracket tags
    parts = [p.strip() for p in s.split(',') if p.strip()]
    addr = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    return addr, city

# Cache for uploaded image URLs - populated on first use, persists for process lifetime
_CACHED_IMAGE_URLS = {}

def _upload_logo_to_drive(image_filename: str = "mohr-partners-logo.png") -> str:
    """
    Get or upload image to Google Drive and return public direct image URL.

    1. Checks in-memory cache first (fastest)
    2. Searches Drive for existing file by name (avoids duplicates)
    3. Uploads new file only if not found
    """
    global _CACHED_IMAGE_URLS

    # Check in-memory cache first - return immediately if we have a URL
    if image_filename in _CACHED_IMAGE_URLS:
        return _CACHED_IMAGE_URLS[image_filename]

    try:
        from .file_handling import ensure_drive_folder
        from .clients import _helper_google_creds
        from googleapiclient.discovery import build

        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_id = ensure_drive_folder()

        # Search for existing file by name in our folder
        query = f"name = '{image_filename}' and trashed = false"
        if folder_id:
            query += f" and '{folder_id}' in parents"

        results = drive.files().list(
            q=query,
            fields="files(id, webViewLink)",
            pageSize=1
        ).execute()

        existing_files = results.get("files", [])

        if existing_files:
            # File already exists - use it
            file_id = existing_files[0].get("id")
            direct_link = f"https://drive.google.com/uc?export=view&id={file_id}"
            _CACHED_IMAGE_URLS[image_filename] = direct_link
            print(f"✅ {image_filename} found in Drive (reusing): {direct_link}")
            return direct_link

        # File doesn't exist - upload it
        from googleapiclient.http import MediaIoBaseUpload
        import io

        # Get the directory of this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(current_dir, "assets", "images", image_filename)

        if not os.path.exists(image_path):
            print(f"⚠️ Image not found: {image_path}")
            return ""

        # Read image file
        with open(image_path, "rb") as img_file:
            image_bytes = img_file.read()

        # Determine MIME type from extension
        ext = os.path.splitext(image_filename)[1].lower()
        mime_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif'
        }
        mime_type = mime_types.get(ext, 'image/png')

        # Upload to Drive (reuse drive client and folder_id from above)
        file_metadata = {
            "name": image_filename,
            "parents": [folder_id] if folder_id else []
        }

        media = MediaIoBaseUpload(
            io.BytesIO(image_bytes),
            mimetype=mime_type,
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

        # Convert Drive link to direct image link
        # Drive webViewLink format: https://drive.google.com/file/d/{file_id}/view
        # Direct image format: https://drive.google.com/uc?export=view&id={file_id}
        if web_link and "/file/d/" in web_link:
            file_id = web_link.split("/file/d/")[1].split("/")[0]
            direct_link = f"https://drive.google.com/uc?export=view&id={file_id}"
            # Cache the URL for future use within this process
            _CACHED_IMAGE_URLS[image_filename] = direct_link
            print(f"✅ {image_filename} uploaded to Drive (cached): {direct_link}")
            return direct_link

        # Cache whatever we got
        if web_link:
            _CACHED_IMAGE_URLS[image_filename] = web_link
        return web_link or ""
    except Exception as e:
        print(f"⚠️ Failed to upload {image_filename} to Drive: {e}")
        # Don't cache failures - allow retry on next call
        return ""

def _image_to_base64(image_path: str) -> str:
    """Convert image file to base64 data URI for email embedding."""
    try:
        # Get the directory of this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(current_dir, "assets", "images", image_path)

        if not os.path.exists(full_path):
            print(f"⚠️ Image not found: {full_path}")
            return ""

        with open(full_path, "rb") as img_file:
            img_data = img_file.read()
            img_base64 = base64.b64encode(img_data).decode('utf-8')

            # Determine MIME type from extension
            ext = os.path.splitext(image_path)[1].lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif'
            }
            mime_type = mime_types.get(ext, 'image/png')

            return f"data:{mime_type};base64,{img_base64}"
    except Exception as e:
        print(f"⚠️ Failed to encode image {image_path}: {e}")
        return ""


_SIGNATURE_DATA_IMAGE_RX = re.compile(
    r"^data:(image/(?:png|jpe?g|gif|webp));base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


def _compress_signature_image(content_type: str, content_bytes_b64: str) -> tuple[str, str]:
    """Keep inline signature logos below Gmail clipping-prone MIME sizes."""
    normalized_b64 = re.sub(r"\s+", "", content_bytes_b64 or "")
    try:
        raw_bytes = base64.b64decode(normalized_b64)
    except Exception:
        return content_type, normalized_b64

    if len(raw_bytes) <= SIGNATURE_INLINE_IMAGE_MAX_BYTES:
        return content_type, normalized_b64

    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        print(f"⚠️ Pillow unavailable for signature logo resize: {exc}")
        return content_type, normalized_b64

    try:
        source = Image.open(io.BytesIO(raw_bytes))
        source = ImageOps.exif_transpose(source)
    except Exception as exc:
        print(f"⚠️ Could not resize signature logo: {exc}")
        return content_type, normalized_b64

    has_alpha = source.mode in {"RGBA", "LA"} or (
        source.mode == "P" and "transparency" in source.info
    )
    best = (content_type, raw_bytes)

    for dimension in (SIGNATURE_INLINE_IMAGE_MAX_DIMENSION, 200, 160, 120, 96):
        image = source.copy()
        image.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        if has_alpha:
            image = image.convert("RGBA")
            image.save(output, format="PNG", optimize=True)
            candidate_type = "image/png"
        else:
            image = image.convert("RGB")
            image.save(output, format="JPEG", quality=82, optimize=True)
            candidate_type = "image/jpeg"

        candidate_bytes = output.getvalue()
        if len(candidate_bytes) < len(best[1]):
            best = (candidate_type, candidate_bytes)
        if len(candidate_bytes) <= SIGNATURE_INLINE_IMAGE_MAX_BYTES:
            return candidate_type, base64.b64encode(candidate_bytes).decode("ascii")

    return best[0], base64.b64encode(best[1]).decode("ascii")


def _has_html_signature(custom_signature: str = None) -> bool:
    signature = (custom_signature or "").strip()
    return bool(signature and ("data-sitesift-professional-signature" in signature or _html_rx.search(signature)))


def _sanitize_custom_signature_html(custom_signature: str) -> str:
    """Keep generated signature HTML email-safe before embedding it in Graph messages."""
    soup = BeautifulSoup(custom_signature or "", "html.parser")
    for unsafe in soup.find_all(["script", "style", "iframe", "object", "embed"]):
        unsafe.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            attr_lower = attr.lower()
            value = tag.get(attr)
            if attr_lower.startswith("on"):
                del tag.attrs[attr]
                continue
            if attr_lower in {"href", "src"} and isinstance(value, str) and value.strip().lower().startswith("javascript:"):
                del tag.attrs[attr]
                continue
            if attr_lower == "src" and tag.name == "img" and isinstance(value, str):
                allowed = re.match(r"^(data:image/(?:png|jpe?g|gif|webp);base64,|cid:|https?://)", value.strip(), re.IGNORECASE)
                if not allowed:
                    del tag.attrs[attr]

    return str(soup)


def _custom_signature_attachment_entries(custom_signature: str = None) -> List[dict]:
    if not custom_signature or not _has_html_signature(custom_signature):
        return []

    soup = BeautifulSoup(_sanitize_custom_signature_html(custom_signature), "html.parser")
    attachments = []
    image_index = 1
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        match = _SIGNATURE_DATA_IMAGE_RX.match(src)
        if not match:
            continue

        content_type = match.group(1).lower().replace("jpg", "jpeg")
        content_type, content_bytes = _compress_signature_image(content_type, match.group(2))
        content_id = f"signature-custom-logo-{image_index}"
        attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f"{content_id}.{content_type.split('/')[-1]}",
            "contentType": content_type,
            "contentBytes": content_bytes,
            "contentId": content_id,
            "isInline": True
        })
        image_index += 1

    return attachments


def _custom_signature_html_with_cids(custom_signature: str = None) -> str:
    if not custom_signature:
        return ""

    soup = BeautifulSoup(_sanitize_custom_signature_html(custom_signature), "html.parser")
    image_index = 1
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not _SIGNATURE_DATA_IMAGE_RX.match(src):
            continue
        img["src"] = f"cid:signature-custom-logo-{image_index}"
        image_index += 1

    return str(soup)


def _legacy_mohr_signature_attachments() -> List[dict]:
    """
    Get signature images as inline attachments for Microsoft Graph API.
    Returns list of attachment objects with contentId for CID references.

    These attachments should be added to the email via Graph API's attachments endpoint
    after creating the draft, allowing the HTML to reference them via cid: URLs.
    """
    attachments = []
    current_dir = os.path.dirname(os.path.abspath(__file__))

    images = [
        {"filename": "mohr-partners-logo.png", "content_id": "signature-logo"},
        {"filename": "linkedin.png", "content_id": "signature-linkedin"},
    ]

    for img in images:
        full_path = os.path.join(current_dir, "assets", "images", img["filename"])

        if not os.path.exists(full_path):
            print(f"⚠️ Signature image not found: {full_path}")
            continue

        try:
            with open(full_path, "rb") as f:
                content_bytes = base64.b64encode(f.read()).decode('utf-8')

            # Determine MIME type
            ext = os.path.splitext(img["filename"])[1].lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif'
            }
            content_type = mime_types.get(ext, 'image/png')

            # Create Graph API attachment object for inline image
            attachment = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": img["filename"],
                "contentType": content_type,
                "contentBytes": content_bytes,
                "contentId": img["content_id"],
                "isInline": True
            }
            attachments.append(attachment)

        except Exception as e:
            print(f"⚠️ Failed to read signature image {img['filename']}: {e}")

    return attachments


def get_signature_attachments(custom_signature: str = None, signature_mode: str = None, user_email: str = None) -> List[dict]:
    """
    Get signature images as inline attachments for Microsoft Graph API.

    User-created professional signatures can include uploaded logo data URLs.
    Those are converted to CID attachments. The bundled MOHR logo is only for
    Jill's explicit legacy profile, never a generic company/domain default.
    """
    custom_attachments = _custom_signature_attachment_entries(custom_signature)
    if custom_attachments:
        return custom_attachments

    if (
        signature_mode == "professional"
        and not (custom_signature and custom_signature.strip())
        and _is_legacy_mohr_signature_user(user_email)
    ):
        return _legacy_mohr_signature_attachments()

    return []

def convert_plain_text_signature_to_html(plain_text_signature: str) -> str:
    """
    Converts a plain text email signature to HTML format.
    Preserves line breaks and wraps in a styled container.
    """
    if not plain_text_signature or not plain_text_signature.strip():
        return ""

    if _has_html_signature(plain_text_signature):
        return _custom_signature_html_with_cids(plain_text_signature)

    # Convert line breaks to HTML
    html_signature = plain_text_signature.replace('\n', '<br>')

    # Wrap in styled container
    return f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; line-height: 1.6;">
{html_signature}
</div>"""


def _is_legacy_mohr_signature_user(user_email: str = None) -> bool:
    """Only Jill's explicit legacy sender profile may use the old hardcoded footer."""
    email = (user_email or "").strip().lower()
    return email == "jill.ames@mohrpartners.com"


def get_email_footer(custom_signature: str = None, signature_mode: str = None, user_email: str = None) -> str:
    """
    Returns HTML formatted email footer.

    Args:
        custom_signature: Optional plain text signature from user settings.
        signature_mode: Signature mode - "none", "custom", or "professional".
                       - "none": No signature at all
                       - "custom": Use the custom_signature text (converted to HTML)
                       - "professional": Use custom_signature when present. Empty
                         professional profiles only use the legacy MOHR/Jill footer for
                         Jill's explicit legacy sender profile.
                       - None/empty: Defaults to "none" (user must explicitly configure)
        user_email: Sender profile email used to gate the legacy Jill footer.
    """
    # Default to "none" when mode is not set - user must explicitly configure signature in settings
    if not signature_mode:
        return ""

    # Handle explicit signature modes
    if signature_mode == "none":
        return ""

    if signature_mode == "custom":
        if custom_signature and custom_signature.strip():
            return convert_plain_text_signature_to_html(custom_signature)
        # Custom mode but no signature text - return empty
        return ""

    if signature_mode == "professional":
        if custom_signature and custom_signature.strip():
            return convert_plain_text_signature_to_html(custom_signature)
        if not _is_legacy_mohr_signature_user(user_email):
            return ""
    else:
        # Unknown mode - treat as none
        return ""

    # Build the footer HTML matching the professional signature layout
    # Uses CID references for images - the actual attachments are added by send_and_index_email()
    # CID (Content-ID) is the most reliable way to embed images in emails across all clients
    footer = """Best,<br>
<br>
<table cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse; margin-top: 10px; font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000;">
<tr>
<td valign="top" style="padding-right: 30px; vertical-align: top;">
<a href="https://mohrpartners.com/" target="_blank" style="text-decoration: none;">
<img src="cid:signature-logo" alt="Mohr Partners" style="width: 120px; height: auto; display: block; border: 0;" />
</a>
</td>
<td valign="top" style="vertical-align: top; font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000;">
<table cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse; width: 100%;">
<tr>
<td colspan="2" style="padding-bottom: 8px;">
<strong style="font-size: 12pt; font-weight: bold; color: #000000;">Jill Ames</strong><br>
<span style="font-size: 10pt; color: #000000;">Senior Associate</span><br>
<span style="font-size: 10pt; color: #000000;">National Accounts</span><br>
<span style="font-size: 10pt; color: #000000;">License Nos. 127384 (WA), SP24646 (ID)</span>
</td>
</tr>
<tr>
<td colspan="2" style="padding: 8px 0;">
<div style="border-top: 1px solid #CC0000; width: 100%;"></div>
</td>
</tr>
<tr>
<td valign="top" style="padding-right: 30px; vertical-align: top; font-size: 10pt; color: #000000;">
T +1 206 510 5575<br>
<a href="mailto:jill.ames@mohrpartners.com" style="color: #000000; text-decoration: underline; text-decoration-color: #CC0000; text-underline-offset: 2px;">jill.ames@mohrpartners.com</a><br>
<a href="https://www.linkedin.com/company/mohr-partners" target="_blank" style="text-decoration: none; display: inline-block; margin-top: 4px;"><img src="cid:signature-linkedin" alt="LinkedIn" style="width: 20px; height: 20px; border: 0; vertical-align: middle;" /></a>
</td>
<td valign="top" style="vertical-align: top; font-size: 10pt; color: #000000;">
<strong style="font-weight: bold; color: #000000;">Mohr Partners, Inc.</strong><br>
<a href="https://mohrpartners.com/" target="_blank" style="color: #CC0000; text-decoration: underline; text-decoration-color: #CC0000; text-underline-offset: 2px;">mohrpartners.com</a><br>
<span style="color: #000000;">Seattle, WA</span>
</td>
</tr>
</table>
</td>
</tr>
</table>"""

    return footer


def needs_signature_attachments(signature_mode: str, custom_signature: str = None, user_email: str = None) -> bool:
    """Check if the signature mode requires inline image attachments."""
    if _custom_signature_attachment_entries(custom_signature):
        return True

    return (
        signature_mode == "professional"
        and not (custom_signature and custom_signature.strip())
        and _is_legacy_mohr_signature_user(user_email)
    )


def format_email_body_with_footer(
    body: str,
    custom_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
) -> str:
    """
    Converts plain text email body to HTML and appends footer.
    Preserves line breaks and formatting.
    Wraps in proper HTML structure to prevent email clients from collapsing the footer.

    Args:
        body: The email body text
        custom_signature: Optional plain text signature from user settings
        signature_mode: Signature mode - "none", "custom", or "professional"
        user_email: Sender profile email used to gate the legacy MOHR footer
    """
    # Strip trailing whitespace/newlines from body before converting.
    body = normalize_outbound_message_text(body).rstrip()

    # Convert plain text to HTML
    # Replace double newlines with <br><br>, single newlines with <br>
    html_body = body.replace('\n\n', '<br><br>').replace('\n', '<br>')

    # Get the footer (custom or default based on mode)
    footer_html = get_email_footer(custom_signature, signature_mode, user_email=user_email)

    # If no signature at all, just return the body without footer section
    if not footer_html:
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px; font-family: Arial, Helvetica, sans-serif; font-size: 10pt;">
<span style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt;">{html_body}</span>
</div>
</body>
</html>"""

    # Wrap in proper HTML structure to prevent email clients from collapsing footer
    # Apply font-family inline to every level (Outlook ignores parent styles)
    full_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px; font-family: Arial, Helvetica, sans-serif; font-size: 10pt;">
<span style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt;">{html_body}</span>
<br><br>
<div style="min-height: 1px;">
{footer_html}
</div>
</div>
</body>
</html>"""

    return full_content
