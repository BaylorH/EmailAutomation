import os
import base64
import hashlib
import ipaddress
import re
import requests
import socket
import tempfile
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import unquote, urljoin, urlparse
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
        'id': None,
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
        result['id'] = file_id
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
    """Fetch PDF attachments from current message only.

    Fails CLOSED on Graph/network failure: a 401/403/5xx or network error while
    downloading attachments is surfaced by raising ``requests.exceptions.
    RequestException`` so the caller can distinguish a real download failure
    from a message that genuinely has no attachments. Swallowing the failure and
    returning ``[]`` would be indistinguishable from the clean no-attachments
    case, causing the message to be marked fully processed with the attachment
    silently dropped. Only a healthy 200 response with no PDF attachments
    returns ``[]``.
    """
    base = "https://graph.microsoft.com/v1.0"

    # Download the attachment list. A Graph/network failure MUST propagate;
    # do not collapse it into an empty list.
    resp = requests.get(
        f"{base}/me/messages/{graph_msg_id}/attachments",
        headers=headers,
        timeout=30,
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

def upload_pdf_to_drive(name: str, content: bytes, folder_id: str = None) -> Optional[str]:
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


PROPERTY_PREVIEW_POSITIVE_TERMS = (
    "sf",
    "available",
    "clear height",
    "clear ht",
    "dock",
    "drive-in",
    "drive in",
    "office",
    "warehouse",
    "nnn",
    "lease",
    "parking",
    "sprinkler",
    "power",
    "industrial",
)

PROPERTY_PREVIEW_NEGATIVE_TERMS = (
    "tour packet",
    "prepared for",
    "prepared by",
    "table of contents",
    "map overview",
    "route map",
    "campaign report",
    "confidential",
)
SAFE_PREVIEW_SIGNAL_KEYS = (
    "imageAreaRatio",
    "textChars",
    "positiveTerms",
    "negativeTerms",
)
MAX_LINKED_PROPERTY_ASSET_BYTES = int(os.getenv("LINKED_PROPERTY_ASSET_MAX_BYTES", str(20 * 1024 * 1024)))
def _safe_preview_signal_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [
            item
            for item in value[:12]
            if isinstance(item, (str, int, float, bool))
        ]
    return None


def _safe_preview_signals(signals: Any) -> Dict[str, Any]:
    if not isinstance(signals, dict):
        return {}
    safe = {}
    for key in SAFE_PREVIEW_SIGNAL_KEYS:
        value = _safe_preview_signal_value(signals.get(key))
        if value is not None:
            safe[key] = value
    return safe


def _resize_png_preview(preview_bytes: bytes, max_dimension: int = 1400) -> bytes:
    if not (HAS_PILLOW and max_dimension and preview_bytes):
        return preview_bytes

    try:
        image = Image.open(io.BytesIO(preview_bytes))
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension))
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception as e:
        print(f"⚠️ Could not resize PDF preview: {e}")
    return preview_bytes


def _text_terms(text: str, terms: Tuple[str, ...]) -> List[str]:
    lowered = f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())} "
    found = []
    for term in terms:
        normalized = f" {re.sub(r'[^a-z0-9]+', ' ', term.lower()).strip()} "
        if normalized in lowered:
            found.append(term)
    return found


def _page_visual_area_ratio(page) -> float:
    try:
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        visual_area = 0.0
        text_dict = page.get_text("dict") or {}
        for block in text_dict.get("blocks", []):
            if block.get("type") == 1 and block.get("bbox"):
                x0, y0, x1, y1 = block["bbox"]
                visual_area += max(0.0, float(x1 - x0)) * max(0.0, float(y1 - y0))
        for drawing in page.get_drawings() or []:
            rect = drawing.get("rect")
            if rect:
                visual_area += max(0.0, float(rect.width)) * max(0.0, float(rect.height))
        return min(visual_area / page_area, 1.0)
    except Exception:
        return 0.0


def _score_pdf_preview_page(page, index: int, page_count: int) -> Dict[str, Any]:
    try:
        text = page.get_text("text") or ""
    except Exception:
        text = ""

    positive_terms = _text_terms(text, PROPERTY_PREVIEW_POSITIVE_TERMS)
    negative_terms = _text_terms(text, PROPERTY_PREVIEW_NEGATIVE_TERMS)
    image_area_ratio = _page_visual_area_ratio(page)
    score = (
        len(positive_terms) * 2.5
        + min(len(text.strip()) / 250.0, 3.0)
        + image_area_ratio * 8.0
        + (0.75 if index > 0 else 0.0)
        - len(negative_terms) * 4.0
    )

    return {
        "index": index,
        "score": round(score, 3),
        "signals": {
            "imageAreaRatio": round(image_area_ratio, 4),
            "textChars": len(text.strip()),
            "positiveTerms": positive_terms[:8],
            "negativeTerms": negative_terms[:8],
        },
    }


def render_pdf_property_preview(
    content: bytes,
    max_dimension: int = 1400,
    max_pages_to_scan: int = 8,
) -> Optional[Dict[str, Any]]:
    """Render the best property/detail PDF page to a PNG preview plus safe metadata."""
    if not content or not HAS_PYMUPDF:
        return None

    try:
        doc = fitz.open(stream=content, filetype="pdf")
        try:
            page_count = len(doc)
            if page_count < 1:
                return None

            scanned_count = max(1, min(page_count, max_pages_to_scan or page_count))
            scored_pages = [
                _score_pdf_preview_page(doc[index], index, page_count)
                for index in range(scanned_count)
            ]
            selected = max(scored_pages, key=lambda item: (item["score"], item["index"]))
            selected_page = doc[selected["index"]]
            matrix = fitz.Matrix(2, 2)
            pix = selected_page.get_pixmap(matrix=matrix, alpha=False)
            preview_bytes = _resize_png_preview(pix.tobytes("png"), max_dimension=max_dimension)
            page_number = selected["index"] + 1
            reason = "selected page with property-detail text"
            if selected["signals"]["imageAreaRatio"] >= 0.1:
                reason = "selected page with property-detail text and large visual area"
            elif page_number == 1:
                reason = "fallback to first available preview page"

            return {
                "bytes": preview_bytes,
                "pageNumber": page_number,
                "pageIndex": selected["index"],
                "pageCount": page_count,
                "strategy": "property_preview_heuristic_v1",
                "selectionReason": reason,
                "score": selected["score"],
                "signals": _safe_preview_signals(selected["signals"]),
            }
        finally:
            doc.close()
    except Exception as e:
        print(f"⚠️ Failed to render PDF preview: {e}")
        return None


def render_pdf_first_page_preview(content: bytes, max_dimension: int = 1400) -> Optional[bytes]:
    """Render the first PDF page to a PNG preview for legacy callers."""
    if not content or not HAS_PYMUPDF:
        return None

    try:
        doc = fitz.open(stream=content, filetype="pdf")
        try:
            if len(doc) < 1:
                return None

            page = doc[0]
            matrix = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            return _resize_png_preview(pix.tobytes("png"), max_dimension=max_dimension)
        finally:
            doc.close()
    except Exception as e:
        print(f"⚠️ Failed to render PDF preview: {e}")
        return None


def upload_property_image_to_drive(name: str, content: bytes, folder_id: str = None) -> Optional[Dict[str, Any]]:
    """Upload a generated property preview image and return safe hosted metadata."""
    if not content:
        return None

    try:
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)

        if not folder_id:
            folder_id = ensure_drive_folder()

        base_name = os.path.splitext(name or "property-preview.pdf")[0].strip() or "property-preview"
        image_name = f"{base_name} preview.png"
        file_metadata = {
            "name": image_name,
            "parents": [folder_id] if folder_id else [],
        }

        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="image/png",
            resumable=True,
        )

        file = drive.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,webViewLink",
        ).execute()

        drive.permissions().create(
            fileId=file.get("id"),
            body={"role": "reader", "type": "anyone"},
        ).execute()

        file_id = file.get("id")
        if not file_id:
            return None

        direct_url = f"https://drive.google.com/uc?export=view&id={file_id}"
        result = {
            "url": direct_url,
            "driveLink": file.get("webViewLink") or direct_url,
            "contentType": "image/png",
            "byteCount": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        print(f"🖼️ Uploaded property preview: {image_name} -> {direct_url}")
        return result

    except Exception as e:
        print(f"❌ Failed to upload property preview image: {e}")
        return None


def _filename_from_asset_url(url: str, fallback: str = "broker flyer.pdf") -> str:
    try:
        path = unquote(urlparse(url or "").path or "")
        name = os.path.basename(path).strip()
        if name:
            return name
    except Exception:
        pass
    return fallback


def _is_public_ip_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _is_blocked_linked_asset_host(host: str) -> bool:
    if not host:
        return True
    try:
        from .property_images import is_blocked_listing_url

        if is_blocked_listing_url(f"https://{host}/"):
            return True
    except Exception:
        pass
    return False


def _validate_public_https_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("linked property asset URL must use https")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("linked property asset URL is missing a host")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("linked property asset URL points to a local host")
    if _is_blocked_linked_asset_host(host):
        raise ValueError("linked property asset host is blocked")

    try:
        literal_ip = ipaddress.ip_address(host)
        if not literal_ip.is_global:
            raise ValueError("linked property asset URL points to a private or reserved address")
        return url
    except ValueError as exc:
        if "private or reserved" in str(exc):
            raise

    try:
        address_infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"linked property asset host could not be resolved: {host}") from exc

    resolved_ips = {info[4][0] for info in address_infos if info and len(info) >= 5 and info[4]}
    if not resolved_ips:
        raise ValueError(f"linked property asset host had no resolved addresses: {host}")

    for ip_text in resolved_ips:
        if not _is_public_ip_address(ip_text):
            raise ValueError("linked property asset URL resolves to a private or reserved address")

    return url


def _download_linked_asset(download_url: str) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SiteSiftAI/1.0; property-image-resolver)"
    }
    current_url = _validate_public_https_url(download_url)
    response = None

    for _ in range(6):
        response = requests.get(
            current_url,
            headers=headers,
            timeout=30,
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400 and response.headers.get("location"):
            current_url = _validate_public_https_url(urljoin(current_url, response.headers["location"]))
            continue
        break
    else:
        raise ValueError("linked property asset redirected too many times")

    response.raise_for_status()
    final_url = getattr(response, "url", current_url)
    if final_url:
        _validate_public_https_url(final_url)

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            expected_bytes = int(content_length)
            if expected_bytes > MAX_LINKED_PROPERTY_ASSET_BYTES:
                raise ValueError(f"linked property asset is too large ({expected_bytes} bytes)")
        except ValueError as exc:
            if "too large" in str(exc):
                raise

    chunks = []
    total_bytes = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > MAX_LINKED_PROPERTY_ASSET_BYTES:
            raise ValueError(f"linked property asset is too large ({total_bytes} bytes)")
        chunks.append(chunk)

    return b"".join(chunks), (response.headers.get("content-type") or "").lower()


def _attach_pdf_property_preview(
    result: Dict[str, Any],
    name: str,
    content: bytes,
    *,
    source_label_prefix: str,
    source_type: str,
) -> None:
    try:
        preview = render_pdf_property_preview(content)
        if not preview:
            legacy_preview_bytes = render_pdf_first_page_preview(content)
            preview = {
                "bytes": legacy_preview_bytes,
                "pageNumber": 1,
                "pageIndex": 0,
                "pageCount": 1,
                "strategy": "first_page_preview_fallback",
                "selectionReason": "fallback to first available preview page",
                "score": 0,
                "signals": {},
            } if legacy_preview_bytes else None
        if preview and preview.get("bytes"):
            uploaded_preview = upload_property_image_to_drive(name, preview["bytes"])
            if uploaded_preview and uploaded_preview.get("url"):
                result["property_image_url"] = uploaded_preview["url"]
                result["property_image_source"] = f"{source_label_prefix}: {name}, page {preview.get('pageNumber') or 1}"
                result["property_image_source_type"] = source_type
                result["property_image_meta"] = {
                    "pageNumber": preview.get("pageNumber") or 1,
                    "pageCount": preview.get("pageCount"),
                    "strategy": preview.get("strategy"),
                    "selectionReason": preview.get("selectionReason"),
                    "score": preview.get("score"),
                    "signals": _safe_preview_signals(preview.get("signals")),
                    "contentType": uploaded_preview.get("contentType") or "image/png",
                    "byteCount": uploaded_preview.get("byteCount"),
                    "sha256": uploaded_preview.get("sha256"),
                    "driveLink": uploaded_preview.get("driveLink"),
                }
    except Exception as e:
        print(f"⚠️ Property preview image resolution failed: {e}")


def _image_link_to_png_preview(content: bytes) -> Optional[bytes]:
    if not (content and HAS_PILLOW):
        return None
    try:
        image = Image.open(io.BytesIO(content))
        image.thumbnail((1400, 1400))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"⚠️ Could not normalize linked image preview: {e}")
        return None


def fetch_and_process_linked_assets(
    urls: List[str],
    max_assets: int = 3,
    target_property_hint: str = "",
) -> List[Dict[str, Any]]:
    """Process safe broker-provided PDF/image links into the same manifest shape as attachments.

    ``target_property_hint`` is the current row's property address/context.
    When provided, links whose filename/URL names a clearly different street
    address are rejected by build_download_candidate's deterministic guard so
    a forwarded wrong-property flyer never populates the row.
    """
    try:
        from .property_images import build_download_candidate
    except Exception as e:
        print(f"⚠️ Could not import property image URL helpers: {e}")
        return []

    processed: List[Dict[str, Any]] = []
    seen_urls = set()
    for raw_url in urls or []:
        if len(processed) >= max_assets:
            break
        source_url = str(raw_url or "").strip()
        if not source_url or source_url in seen_urls:
            continue
        seen_urls.add(source_url)

        filename_hint = _filename_from_asset_url(source_url, fallback="")
        candidate = build_download_candidate(
            source_url,
            filename_hint=filename_hint,
            target_property_hint=target_property_hint,
        )
        if not candidate:
            continue

        name = _filename_from_asset_url(candidate.get("sourceUrl") or source_url, filename_hint or "broker flyer.pdf")
        if candidate.get("sourceType") == "direct_image" and not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            name = "broker property image.png"

        # File-share links (SharePoint/OneDrive/Box/WeTransfer/Drive folder) cannot
        # be auto-downloaded to a concrete file. Surface them as a distinguishable
        # manual-review entry rather than silently dropping the broker's payload —
        # a dropped link is indistinguishable from 'no assets' and lets the message
        # be marked processed with the broker's data lost.
        if candidate.get("requiresManualReview") or not candidate.get("downloadUrl"):
            print(f"⚠️ Broker file-share link needs manual review (not auto-downloadable): {source_url}")
            processed.append({
                "name": name,
                "text": "",
                "images": [],
                "method": "manual_review_required",
                "source_url": source_url,
                "source_type": candidate.get("sourceType") or "broker_file_share_link",
                "drive_link": None,
                "requires_manual_review": True,
                "error": "Broker file-share link could not be auto-downloaded; needs manual review",
            })
            continue

        try:
            content, content_type = _download_linked_asset(candidate["downloadUrl"])
        except Exception as e:
            # A broken/protected broker link (dead link, 403 protected Drive file)
            # MUST stay visible. Swallowing it and continuing (returning []) is
            # indistinguishable from 'no assets' and lets process_inbox_message see
            # no error and mark the message processed — the broker's payload is lost
            # with no retry/visibility. Surface a distinguishable failure entry.
            print(f"⚠️ Failed to download linked property asset {source_url}: {e}")
            processed.append({
                "name": name,
                "text": "",
                "images": [],
                "method": "failed",
                "source_url": source_url,
                "source_type": candidate.get("sourceType") or "",
                "drive_link": None,
                "download_failed": True,
                "error": str(e),
            })
            continue

        source_type = candidate.get("sourceType") or ""
        is_pdf = source_type.endswith("_pdf") or "pdf" in content_type or name.lower().endswith(".pdf")
        is_image = source_type == "direct_image" or content_type.startswith("image/")

        if is_pdf:
            print(f"\n🔗 Processing linked PDF: {name} ({len(content)} bytes)")
            result = process_pdf_for_ai(content, name)
            result["name"] = name
            result["source_url"] = source_url
            result["source_type"] = source_type
            try:
                result["drive_link"] = upload_pdf_to_drive(name, content)
            except Exception as e:
                print(f"⚠️ Linked PDF Drive upload failed: {e}")
                result["drive_link"] = None
            _attach_pdf_property_preview(
                result,
                name,
                content,
                source_label_prefix="Broker flyer link preview",
                source_type="broker_pdf_link_preview",
            )
            processed.append(result)
        elif is_image:
            preview_bytes = _image_link_to_png_preview(content)
            if not preview_bytes:
                continue
            print(f"\n🔗 Processing linked property image: {name} ({len(content)} bytes)")
            uploaded_preview = upload_property_image_to_drive(name, preview_bytes)
            if not (uploaded_preview and uploaded_preview.get("url")):
                continue
            processed.append({
                "name": name,
                "text": "",
                "images": [],
                "method": "direct_image_link",
                "source_url": source_url,
                "source_type": source_type,
                "drive_link": None,
                "property_image_url": uploaded_preview["url"],
                "property_image_source": f"Broker image link: {name}",
                "property_image_source_type": "broker_image_link",
                "property_image_meta": {
                    "strategy": "direct_image_link_v1",
                    "selectionReason": "broker-provided public image link",
                    "contentType": uploaded_preview.get("contentType") or "image/png",
                    "byteCount": uploaded_preview.get("byteCount"),
                    "sha256": uploaded_preview.get("sha256"),
                    "driveLink": uploaded_preview.get("driveLink"),
                },
            })

    if processed:
        print(f"🖼️ Resolved {len(processed)} linked property asset(s)")
    return processed


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

        if result.get('method') == 'failed':
            # Total extraction failure: local text extraction yielded nothing AND
            # the OpenAI upload fallback failed (no file_id, no text). Handing this
            # downstream as a normal manifest entry — with a drive_link — would
            # write a flyer link to the row and let the message be marked processed
            # though ZERO specs were extracted, hiding a complete extraction
            # failure. Surface it as a distinguishable failure marker instead (no
            # drive_link, no property preview) so it is not mistaken for a usable
            # result.
            print(f"❌ PDF extraction failed for {name}; surfacing as failure (not a usable manifest entry)")
            processed.append({
                "name": name,
                "text": "",
                "images": result.get("images") or [],
                "method": "failed_extraction",
                "file_id": None,
                "id": None,
                "drive_link": None,
                "extraction_failed": True,
                "error": "PDF text extraction and OpenAI upload both failed",
            })
            continue

        # Upload to Drive for archival
        try:
            drive_link = upload_pdf_to_drive(name, content)
            result['drive_link'] = drive_link
        except Exception as e:
            print(f"⚠️ Drive upload failed: {e}")
            result['drive_link'] = None

        _attach_pdf_property_preview(
            result,
            name,
            content,
            source_label_prefix="Broker flyer preview",
            source_type="broker_pdf_preview",
        )

        processed.append(result)

    return processed
