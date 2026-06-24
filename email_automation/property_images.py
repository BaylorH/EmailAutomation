import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


PROPERTY_IMAGE_COLUMN = "Property Image"
PROPERTY_IMAGE_SOURCE_COLUMN = "Property Image Source"
PROPERTY_IMAGE_SOURCE_REASON = "Broker flyer preview image resolved from attachment."
SAFE_SIGNAL_KEYS = (
    "imageAreaRatio",
    "textChars",
    "positiveTerms",
    "negativeTerms",
)

BLOCKED_LISTING_DOMAINS = (
    "costar.com",
    "loopnet.com",
)
DIRECT_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
)
SAFE_DIRECT_IMAGE_HOSTS = (
    "drive.google.com",
    "googleusercontent.com",
    "dropbox.com",
    "dropboxusercontent.com",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_signal_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [
            item
            for item in value[:12]
            if isinstance(item, (str, int, float, bool))
        ]
    return None


def _safe_signals(signals: Any) -> Dict[str, Any]:
    if not isinstance(signals, dict):
        return {}
    safe = {}
    for key in SAFE_SIGNAL_KEYS:
        value = _safe_signal_value(signals.get(key))
        if value is not None:
            safe[key] = value
    return safe


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _is_safe_direct_image_host(host: str) -> bool:
    return any(_host_matches(host, domain) for domain in SAFE_DIRECT_IMAGE_HOSTS)


def _looks_like_image_asset(source_url: str, filename_hint: str = "") -> bool:
    lower_hint = _clean(filename_hint).lower()
    if lower_hint.endswith(DIRECT_IMAGE_EXTENSIONS):
        return True
    try:
        lower_path = urlparse(source_url).path.lower()
    except Exception:
        lower_path = ""
    return lower_path.endswith(DIRECT_IMAGE_EXTENSIONS)


def is_blocked_listing_url(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in BLOCKED_LISTING_DOMAINS)


def _drive_file_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if match:
        return match.group(1)
    if parsed.netloc.lower().endswith("drive.google.com"):
        params = dict(parse_qsl(parsed.query))
        if params.get("id"):
            return params["id"]
    return None


def _dropbox_download_url(url: str) -> str:
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["dl"] = "1"
    return urlunparse(parsed._replace(query=urlencode(params)))


def build_download_candidate(url: str, filename_hint: str = "") -> Optional[Dict[str, Any]]:
    source_url = _clean(url)
    if not source_url or is_blocked_listing_url(source_url):
        return None

    file_name = _clean(filename_hint) or "broker attachment"
    host = _host(source_url)
    is_direct_image = (
        _is_safe_direct_image_host(host)
        and (_looks_like_image_asset(source_url, file_name) or host.endswith("googleusercontent.com"))
    )
    drive_id = _drive_file_id(source_url)
    if drive_id:
        if is_direct_image:
            return {
                "downloadUrl": f"https://drive.google.com/uc?export=download&id={drive_id}",
                "sourceType": "direct_image",
                "sourceLabel": f"Broker image: {file_name}",
                "sourceUrl": source_url,
            }
        return {
            "downloadUrl": f"https://drive.google.com/uc?export=download&id={drive_id}",
            "sourceType": "google_drive_pdf",
            "sourceLabel": f"Broker flyer: {file_name}",
            "sourceUrl": source_url,
        }

    if host.endswith("dropbox.com"):
        if is_direct_image:
            return {
                "downloadUrl": _dropbox_download_url(source_url),
                "sourceType": "direct_image",
                "sourceLabel": f"Broker image: {file_name}",
                "sourceUrl": source_url,
            }
        return {
            "downloadUrl": _dropbox_download_url(source_url),
            "sourceType": "dropbox_pdf",
            "sourceLabel": f"Broker flyer: {file_name}",
            "sourceUrl": source_url,
        }

    if is_direct_image:
        return {
            "downloadUrl": source_url,
            "sourceType": "direct_image",
            "sourceLabel": f"Broker image: {file_name}",
            "sourceUrl": source_url,
        }

    return None


def _safe_candidate_from_manifest_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    image_url = _clean(item.get("property_image_url"))
    if not image_url:
        return None

    meta = item.get("property_image_meta") or {}
    safe_meta = {
        key: meta.get(key)
        for key in (
            "pageNumber",
            "pageCount",
            "strategy",
            "selectionReason",
            "score",
            "contentType",
            "byteCount",
            "sha256",
            "driveLink",
            "width",
            "height",
        )
        if meta.get(key) is not None
    }
    safe_signals = _safe_signals(meta.get("signals"))
    if safe_signals:
        safe_meta["signals"] = safe_signals
    return {
        "url": image_url,
        "sourceLabel": _clean(item.get("property_image_source"))
        or f"Broker flyer preview: {_clean(item.get('name')) or 'attachment.pdf'}, page {safe_meta.get('pageNumber') or 1}",
        "sourceType": _clean(item.get("property_image_source_type")) or "broker_pdf_preview",
        "sourceFilename": _clean(item.get("name")),
        "sourceDriveLink": _clean(item.get("drive_link")),
        "meta": safe_meta,
    }


def select_property_image_candidate(pdf_manifest: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in pdf_manifest or []:
        candidate = _safe_candidate_from_manifest_item(item or {})
        if candidate:
            return candidate
    return None


def _header_index(header: List[str], column: str) -> int:
    target = _clean(column).lower()
    for idx, value in enumerate(header or []):
        if _clean(value).lower() == target:
            return idx
    return -1


def _row_value(header: List[str], rowvals: List[str], column: str) -> str:
    idx = _header_index(header, column)
    if idx < 0 or idx >= len(rowvals or []):
        return ""
    return _clean(rowvals[idx])


def build_property_image_sheet_updates(
    header: List[str],
    rowvals: List[str],
    candidate: Optional[Dict[str, Any]],
) -> Dict[str, List[str]]:
    if not candidate or not _clean(candidate.get("url")):
        return {}
    if _row_value(header, rowvals, PROPERTY_IMAGE_COLUMN):
        return {}

    updates = {PROPERTY_IMAGE_COLUMN: [_clean(candidate.get("url"))]}
    source_label = _clean(candidate.get("sourceLabel"))
    if source_label and not _row_value(header, rowvals, PROPERTY_IMAGE_SOURCE_COLUMN):
        updates[PROPERTY_IMAGE_SOURCE_COLUMN] = [source_label]
    return updates
