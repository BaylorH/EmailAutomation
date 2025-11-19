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
    """Returns HTML formatted email footer for Jill Ames matching the screenshot."""
    # Embed images as base64
    logos_base64 = _image_to_base64("logos.png")
    fb_base64 = _image_to_base64("fb.png")
    x_base64 = _image_to_base64("x.png")
    linkedin_base64 = _image_to_base64("linkedin.png")
    
    # Build the footer HTML matching the screenshot layout
    # Entire footer uses Times New Roman, font size 10
    footer = """<div style="font-family: 'Times New Roman', Times, serif; font-size: 10pt;">
<br><br>
Thanks!<br>
Best,<br>
<br>
<table cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse; margin-top: 10px; font-family: 'Times New Roman', Times, serif; font-size: 10pt;">
<tr>
<td valign="top" style="padding-right: 20px;">
<a href="https://mohrpartners.com/" target="_blank">"""
    
    if logos_base64:
        footer += f'<img src="{logos_base64}" alt="Mohr Partners & NMSDC Certification" style="max-width: 200px; height: auto;" />'
    else:
        footer += "Mohr Partners"
    
    footer += """</a>
</td>
<td valign="top" style="font-family: 'Times New Roman', Times, serif; font-size: 10pt;">
<strong>Jill Ames</strong><br>
Senior Commercial Real Estate Broker | National Accounts<br>
<a href="http://www.jillames.com" target="_blank" style="color: #0000EE; text-decoration: underline;">www.jillames.com</a><br>
Direct: 206 510 5575<br>
<strong>Mohr Partners, Inc.</strong> <a href="https://mohrpartners.com/" target="_blank" style="color: #0000EE; text-decoration: underline;">mohrpartners.com</a><br>
<br>"""
    
    # Social media icons
    if fb_base64 or x_base64 or linkedin_base64:
        footer += '<table cellpadding="0" cellspacing="5" border="0" style="border-collapse: collapse;"><tr>'
        if fb_base64:
            footer += f'<td><a href="https://www.facebook.com/mohrpartnersinc" target="_blank"><img src="{fb_base64}" alt="Facebook" style="width: 24px; height: 24px;" /></a></td>'
        if x_base64:
            # TODO: Update with correct Twitter/X link if different
            footer += f'<td><a href="https://x.com/mohrpartners" target="_blank"><img src="{x_base64}" alt="Twitter/X" style="width: 24px; height: 24px;" /></a></td>'
        if linkedin_base64:
            footer += f'<td><a href="https://www.linkedin.com/company/mohr-partners" target="_blank"><img src="{linkedin_base64}" alt="LinkedIn" style="width: 24px; height: 24px;" /></a></td>'
        footer += '</tr></table><br>'
    
    footer += """</td>
</tr>
</table>
<br>
<div style="font-size: 10pt; color: #666666; line-height: 1.4; font-family: 'Times New Roman', Times, serif;">
My goal is to have a healthy work life balance, so please note that my office hours are Monday through Friday 8am to 5pm MT. Our offices are closed for all major holidays. Emails, voicemails, and text messages that are business related will be returned during my office hours.<br>
<br>
This message and its contents are confidential. If you received this message in error, do not use or rely upon it. Instead, please inform the sender and then delete it. WA License 127384 held by Mohr Partners Inc. Idaho License SP24646 held by Western Idaho Realty, Inc.
</div>
</div>"""
    
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