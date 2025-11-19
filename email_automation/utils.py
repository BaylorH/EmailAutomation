import re
import base64
import time
import requests
import os
from bs4 import BeautifulSoup
from typing import Optional, List

# Helper: detect HTML vs text
_html_rx = re.compile(r"<[a-zA-Z/][^>]*>")

def _body_kind(script: str):
    if script and _html_rx.search(script):
        return "HTML", script
    return "Text", script or ""

def _normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def _norm_txt(x: str) -> str:
    return (x or "").strip().lower()

def b64url_id(message_id: str) -> str:
    """Encode message ID for safe use as Firestore document key."""
    return base64.urlsafe_b64encode(message_id.encode('utf-8')).decode('ascii').rstrip('=')

def normalize_message_id(msg_id: str) -> str:
    """Normalize message ID - keep as-is but strip whitespace."""
    return msg_id.strip() if msg_id else ""

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

def fetch_url_as_text(url: str) -> str | None:
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
    # Trim common trailing junk (punctuation, stray words glued to the URL)
    u = re.sub(r'[\)\]\}\.,;:!?]+$', '', u)
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

def _upload_logo_to_drive(image_filename: str = "logo.png") -> str:
    """Upload image to Google Drive and return public direct image URL."""
    try:
        from .file_handling import ensure_drive_folder
        from .clients import _helper_google_creds
        from googleapiclient.discovery import build
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
        
        # Upload to Drive with correct MIME type
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        folder_id = ensure_drive_folder()
        
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
            print(f"✅ {image_filename} uploaded to Drive: {direct_link}")
            return direct_link
        
        return web_link or ""
    except Exception as e:
        print(f"⚠️ Failed to upload {image_filename} to Drive: {e}")
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

def get_email_footer() -> str:
    """Returns HTML formatted email footer for Jill Ames matching the professional signature style."""
    # Upload logo to Drive and get public URL (more reliable than base64 for email clients)
    logo_url = _upload_logo_to_drive()
    
    # Upload LinkedIn icon to Drive
    linkedin_url = _upload_logo_to_drive("linkedin.png")
    
    # Build the footer HTML matching the professional signature layout
    # Uses sans-serif font (Arial/Helvetica), black text
    footer = """<br><br>
Thanks!<br>
Best,<br>
<br>
<table cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse; margin-top: 10px; font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000;">
<tr>
<td valign="top" style="padding-right: 30px; vertical-align: top;">
<a href="https://mohrpartners.com/" target="_blank" style="text-decoration: none;">"""
    
    if logo_url:
        footer += f'<img src="{logo_url}" alt="Mohr Partners" style="width: 120px; height: auto; display: block; border: 0;" />'
    else:
        footer += "Mohr Partners"
    
    footer += """</a>
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
<a href="mailto:jill.ames@mohrpartners.com" style="color: #000000; text-decoration: underline; text-decoration-color: #CC0000; text-underline-offset: 2px;">jill.ames@mohrpartners.com</a><br>"""
    
    # Add LinkedIn icon below email
    if linkedin_url:
        footer += f'<a href="https://www.linkedin.com/company/mohr-partners" target="_blank" style="text-decoration: none; display: inline-block; margin-top: 4px;"><img src="{linkedin_url}" alt="LinkedIn" style="width: 20px; height: 20px; border: 0; vertical-align: middle;" /></a>'
    
    footer += """</td>
<td valign="top" style="vertical-align: top; font-size: 10pt; color: #000000;">
<strong style="font-weight: bold; color: #000000;">Mohr Partners, Inc.</strong><br>
<a href="https://mohrpartners.com/" target="_blank" style="color: #000000; text-decoration: underline; text-decoration-color: #CC0000; text-underline-offset: 2px;">mohrpartners.com</a><br>
<span style="color: #000000;">Seattle, WA</span>
</td>
</tr>
</table>
</td>
</tr>
</table>"""
    
    return footer

def format_email_body_with_footer(body: str) -> str:
    """
    Converts plain text email body to HTML and appends footer.
    Preserves line breaks and formatting.
    """
    # Convert plain text to HTML
    # Replace double newlines with <br><br>, single newlines with <br>
    html_body = body.replace('\n\n', '<br><br>').replace('\n', '<br>')
    
    # Append footer
    return html_body + get_email_footer()