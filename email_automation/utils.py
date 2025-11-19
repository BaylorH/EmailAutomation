import re
import base64
import time
import requests
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

def get_email_footer() -> str:
    """Returns HTML formatted email footer for Jill Ames."""
    return """<br><br>
Thanks!<br>
Best,<br>
<br>
<strong>Jill Ames</strong><br>
Real Estate Broker<br>
Mohr Partners, Inc.<br>
Direct: 206-510-5575<br>
<a href="http://www.JillAmes.com">www.JillAmes.com</a><br>
<br>
<em>This message and its contents are confidential. If you received this message in error, do not use or rely upon it. Instead, please inform the sender and then delete it. Thank you.</em>"""

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