import os
import base64
import datetime
import hashlib
import ipaddress
import re
import requests
import socket
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import unquote, urljoin, urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
from .clients import _helper_google_creds, client


_PDF_PAGE_MARKER_LINE_RE = re.compile(r"^--- Page [1-9][0-9]* ---$", re.MULTILINE)


def _pdf_substantive_text_for_threshold(extracted_text: str) -> str:
    """Remove exact generated page-marker lines for threshold evaluation."""
    return _PDF_PAGE_MARKER_LINE_RE.sub("", extracted_text).strip()


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
    from PIL import Image, ImageOps
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


NATIVE_IMAGE_FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
NATIVE_IMAGE_EXTENSION_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
NATIVE_IMAGE_MIME_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}
NATIVE_IMAGE_MAX_COUNT = 3
NATIVE_IMAGE_MAX_SOURCE_BYTES = 10 * 1024 * 1024
NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES = 20 * 1024 * 1024
NATIVE_IMAGE_MAX_PIXELS = 20_000_000
NATIVE_IMAGE_MAX_EDGE = 1400
GRAPH_ATTACHMENT_SNAPSHOT_MAX_PAGES = 20
GRAPH_ATTACHMENT_SNAPSHOT_MAX_ITEMS = 100
# Graph attachment names are normally far shorter; 1024 also leaves ample room
# for a sheet-derived complete property anchor while bounding Unicode/regex work.
NATIVE_IMAGE_MAX_ADDRESS_TEXT_CHARS = 1024

_NATIVE_IMAGE_GENERIC_NAME = "Broker property image"
_NATIVE_IMAGE_SAFE_MANIFEST_KEYS = (
    "name",
    "text",
    "method",
    "source_type",
    "property_binding",
    "binding_method",
    "image_meta",
)
_NATIVE_IMAGE_SAFE_META_KEYS = (
    "content_type",
    "width",
    "height",
    "source_bytes",
    "normalized_bytes",
    "normalized_sha256",
)
_NATIVE_IMAGE_BOUND_ASSET_KEYS = frozenset(
    (
        "name",
        "content_type",
        "data",
        "width",
        "height",
        "source_bytes",
        "normalized_bytes",
        "normalized_sha256",
        "property_binding",
        "binding_method",
    )
)
_NATIVE_IMAGE_FAILURE_PRECEDENCE = (
    "image_attachment_too_many",
    "image_attachment_too_large",
    "image_attachment_type_mismatch",
    "image_attachment_invalid_base64",
    "image_attachment_bad_magic",
    "image_attachment_decode_failed",
    "image_attachment_mixed_property",
    "image_attachment_wrong_property",
    "image_attachment_unbound_property",
)
_NATIVE_IMAGE_STREET_SUFFIX_ALIASES = {
    "alley": "aly",
    "aly": "aly",
    "av": "ave",
    "avenue": "ave",
    "ave": "ave",
    "bend": "bnd",
    "bnd": "bnd",
    "boulevard": "blvd",
    "blvd": "blvd",
    "circle": "cir",
    "cir": "cir",
    "court": "ct",
    "ct": "ct",
    "crescent": "cres",
    "cres": "cres",
    "crossing": "xing",
    "xing": "xing",
    "drive": "dr",
    "dr": "dr",
    "expressway": "expy",
    "expy": "expy",
    "freeway": "fwy",
    "fwy": "fwy",
    "highway": "hwy",
    "hwy": "hwy",
    "lane": "ln",
    "ln": "ln",
    "loop": "loop",
    "parkway": "pkwy",
    "pkwy": "pkwy",
    "place": "pl",
    "pl": "pl",
    "pike": "pike",
    "plaza": "plz",
    "plz": "plz",
    "road": "rd",
    "rd": "rd",
    "row": "row",
    "square": "sq",
    "sq": "sq",
    "street": "st",
    "st": "st",
    "terrace": "ter",
    "ter": "ter",
    "trail": "trl",
    "trl": "trl",
    "turnpike": "tpke",
    "tpke": "tpke",
    "way": "way",
}
_NATIVE_IMAGE_DIRECTIONAL_ALIASES = {
    "n": "n",
    "north": "n",
    "ne": "ne",
    "northeast": "ne",
    "e": "e",
    "east": "e",
    "se": "se",
    "southeast": "se",
    "s": "s",
    "south": "s",
    "sw": "sw",
    "southwest": "sw",
    "w": "w",
    "west": "w",
    "nw": "nw",
    "northwest": "nw",
}
_NATIVE_IMAGE_HYPHENATED_DIRECTIONALS = (
    ("north", "east", "ne"),
    ("n", "e", "ne"),
    ("north", "west", "nw"),
    ("n", "w", "nw"),
    ("south", "east", "se"),
    ("s", "e", "se"),
    ("south", "west", "sw"),
    ("s", "w", "sw"),
)
_NATIVE_IMAGE_UNIT_MARKERS = frozenset(
    ("apartment", "apt", "suite", "ste", "unit")
)
_NATIVE_IMAGE_STATE_ALIASES = {
    "alabama": "AL",
    "al": "AL",
    "alaska": "AK",
    "ak": "AK",
    "arizona": "AZ",
    "az": "AZ",
    "arkansas": "AR",
    "ar": "AR",
    "california": "CA",
    "ca": "CA",
    "colorado": "CO",
    "co": "CO",
    "connecticut": "CT",
    "ct": "CT",
    "delaware": "DE",
    "de": "DE",
    "district of columbia": "DC",
    "dc": "DC",
    "florida": "FL",
    "fl": "FL",
    "georgia": "GA",
    "ga": "GA",
    "hawaii": "HI",
    "hi": "HI",
    "idaho": "ID",
    "id": "ID",
    "illinois": "IL",
    "il": "IL",
    "indiana": "IN",
    "in": "IN",
    "iowa": "IA",
    "ia": "IA",
    "kansas": "KS",
    "ks": "KS",
    "kentucky": "KY",
    "ky": "KY",
    "louisiana": "LA",
    "la": "LA",
    "maine": "ME",
    "me": "ME",
    "maryland": "MD",
    "md": "MD",
    "massachusetts": "MA",
    "ma": "MA",
    "michigan": "MI",
    "mi": "MI",
    "minnesota": "MN",
    "mn": "MN",
    "mississippi": "MS",
    "ms": "MS",
    "missouri": "MO",
    "mo": "MO",
    "montana": "MT",
    "mt": "MT",
    "nebraska": "NE",
    "ne": "NE",
    "nevada": "NV",
    "nv": "NV",
    "new hampshire": "NH",
    "nh": "NH",
    "new jersey": "NJ",
    "nj": "NJ",
    "new mexico": "NM",
    "nm": "NM",
    "new york": "NY",
    "ny": "NY",
    "north carolina": "NC",
    "nc": "NC",
    "north dakota": "ND",
    "nd": "ND",
    "ohio": "OH",
    "oh": "OH",
    "oklahoma": "OK",
    "ok": "OK",
    "oregon": "OR",
    "or": "OR",
    "pennsylvania": "PA",
    "pa": "PA",
    "rhode island": "RI",
    "ri": "RI",
    "south carolina": "SC",
    "sc": "SC",
    "south dakota": "SD",
    "sd": "SD",
    "tennessee": "TN",
    "tn": "TN",
    "texas": "TX",
    "tx": "TX",
    "utah": "UT",
    "ut": "UT",
    "vermont": "VT",
    "vt": "VT",
    "virginia": "VA",
    "va": "VA",
    "washington": "WA",
    "wa": "WA",
    "west virginia": "WV",
    "wv": "WV",
    "wisconsin": "WI",
    "wi": "WI",
    "wyoming": "WY",
    "wy": "WY",
}
_NATIVE_IMAGE_JPEG_SOF_MARKERS = frozenset(
    (
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    )
)
_NATIVE_IMAGE_SUFFIX_PATTERN = "|".join(
    re.escape(alias)
    for alias in sorted(
        _NATIVE_IMAGE_STREET_SUFFIX_ALIASES,
        key=lambda value: (len(value.split()), len(value)),
        reverse=True,
    )
)
_NATIVE_IMAGE_UNIT_PATTERN = "|".join(
    re.escape(alias)
    for alias in sorted(_NATIVE_IMAGE_UNIT_MARKERS, key=len, reverse=True)
)
_NATIVE_IMAGE_STATE_PATTERN = "|".join(
    re.escape(alias).replace(r"\ ", r"\s+")
    for alias in sorted(
        _NATIVE_IMAGE_STATE_ALIASES,
        key=lambda value: (len(value.split()), len(value)),
        reverse=True,
    )
)
_NATIVE_IMAGE_STREET_TOKEN_PATTERN = (
    r"(?:[a-z][a-z0-9]*|\d+(?:st|nd|rd|th))"
)
_NATIVE_IMAGE_ADDRESS_RE = re.compile(
    r"(?<![a-z0-9])(?P<number>\d+[a-z]?)\s+"
    rf"(?P<street>(?:{_NATIVE_IMAGE_STREET_TOKEN_PATTERN}\s+){{1,9}}?)"
    rf"(?P<suffix>{_NATIVE_IMAGE_SUFFIX_PATTERN})"
    rf"(?:\s+(?P<unit_marker>{_NATIVE_IMAGE_UNIT_PATTERN})"
    r"\s+(?P<unit>[a-z0-9]+))?"
    rf"\s+(?P<city>(?!(?:{_NATIVE_IMAGE_UNIT_PATTERN})\b)"
    r"[a-z]+(?:\s+[a-z]+){0,5}?)"
    rf"\s+(?P<state>{_NATIVE_IMAGE_STATE_PATTERN})"
    r"\s+(?P<zip>\d{5})(?![a-z0-9])"
    r"(?:\s+\d{4}(?![a-z0-9])|(?!\s+\d{4}))"
)
_NATIVE_IMAGE_PARTIAL_STREET_RE = re.compile(
    r"(?<![a-z0-9])\d+[a-z]?\s+"
    rf"(?:{_NATIVE_IMAGE_STREET_TOKEN_PATTERN}\s+){{1,9}}?"
    rf"(?:{_NATIVE_IMAGE_SUFFIX_PATTERN})\b"
)
_NATIVE_IMAGE_PARTIAL_STATE_ZIP_RE = re.compile(
    rf"\b(?:{_NATIVE_IMAGE_STATE_PATTERN})\s+\d{{5}}"
    r"(?![a-z0-9])(?:\s+\d{4}(?![a-z0-9])|(?!\s+\d{4}))"
)
_NATIVE_IMAGE_PARTIAL_NUMBER_STREET_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])\d+[a-z]?\s+"
    rf"{_NATIVE_IMAGE_STREET_TOKEN_PATTERN}\b"
)
_NATIVE_IMAGE_PARTIAL_STREET_WITHOUT_NUMBER_RE = re.compile(
    rf"(?<![a-z0-9])(?:{_NATIVE_IMAGE_STREET_TOKEN_PATTERN}\s+){{1,9}}"
    rf"(?:{_NATIVE_IMAGE_SUFFIX_PATTERN})\b"
)
_NATIVE_IMAGE_LEADING_ISO_DATE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-+|\s+)"
)
_NATIVE_IMAGE_TRAILING_ISO_DATE_RE = re.compile(
    r"(?:^|[-_\s]+)(?P<date>\d{4}-\d{2}-\d{2})\s*$"
)
_NATIVE_IMAGE_TRAILING_NUMBER_METADATA_RE = re.compile(
    r"(?:^|[-_\s]+)"
    r"(?:(?:copy|page|photo)[-_\s]+\d+|\(\s*\d+\s*\))\s*$"
)
_NATIVE_IMAGE_UNCLAIMED_ALPHANUMERIC_NUMBER_RE = re.compile(
    r"(?<![a-z0-9])[a-z0-9]*\d[a-z0-9]*(?![a-z0-9])"
)


def _native_image_failure(code: str) -> Dict[str, Any]:
    return {
        "status": "quarantined",
        "assets": [],
        "failure": {
            "name": _NATIVE_IMAGE_GENERIC_NAME,
            "code": code,
        },
    }


def _select_native_image_failure(codes) -> Optional[str]:
    present = set(codes or [])
    for code in _NATIVE_IMAGE_FAILURE_PRECEDENCE:
        if code in present:
            return code
    return None


def _native_image_size_failure(
    decoded_sizes: List[int],
    pixel_counts: List[int],
) -> Optional[str]:
    if any(size > NATIVE_IMAGE_MAX_SOURCE_BYTES for size in decoded_sizes):
        return "image_attachment_too_large"
    if sum(decoded_sizes) > NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES:
        return "image_attachment_too_large"
    if any(pixel_count > NATIVE_IMAGE_MAX_PIXELS for pixel_count in pixel_counts):
        return "image_attachment_too_large"
    return None


def _native_image_iso_date_is_valid(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _normalize_native_image_address_text(
    value: Any,
    *,
    strip_extension: bool = False,
) -> Optional[str]:
    """Normalize address punctuation and Unicode with no external lookup."""
    if (
        not isinstance(value, str)
        or len(value) > NATIVE_IMAGE_MAX_ADDRESS_TEXT_CHARS
    ):
        return None
    text = os.path.splitext(value)[0] if strip_extension else value
    for character in text:
        if character.isnumeric() and not character.isdecimal():
            normalized_numeric = unicodedata.normalize("NFKD", character)
            if not normalized_numeric or not all(
                "0" <= normalized_character <= "9"
                for normalized_character in normalized_numeric
            ):
                return None
    text = unicodedata.normalize("NFKD", text)
    characters = []
    for character in text:
        if unicodedata.combining(character):
            continue
        if character.isdecimal():
            character = str(unicodedata.decimal(character))
        characters.append(character)
    text = "".join(characters)
    text = text.casefold()
    date_match = _NATIVE_IMAGE_LEADING_ISO_DATE_RE.match(text)
    if date_match and _native_image_iso_date_is_valid(
        date_match.group("date")
    ):
        text = text[date_match.end():]
    if strip_extension:
        metadata_match = _NATIVE_IMAGE_TRAILING_NUMBER_METADATA_RE.search(
            text
        )
        if metadata_match:
            text = text[:metadata_match.start()]
        else:
            trailing_date_match = _NATIVE_IMAGE_TRAILING_ISO_DATE_RE.search(
                text
            )
            if (
                trailing_date_match
                and _native_image_iso_date_is_valid(
                    trailing_date_match.group("date")
                )
            ):
                text = text[:trailing_date_match.start()]
    text = re.sub(
        r"\b([a-z])\s*\.\s*([a-z])\s*\.?(?=\s|$)",
        r"\1\2",
        text,
    )
    for first, second, canonical in _NATIVE_IMAGE_HYPHENATED_DIRECTIONALS:
        text = re.sub(
            rf"\b{first}\s*-\s*{second}\b",
            canonical,
            text,
        )
    text = re.sub(r"(?<!\d)(\d{5})\s*-\s*(\d{4})(?!\d)", r"\1 \2", text)
    text = re.sub(
        rf"\b({_NATIVE_IMAGE_UNIT_PATTERN})\s+"
        r"([a-z0-9]+)\s*-\s*([a-z][a-z0-9]{0,2})(?=[^a-z0-9]|$)",
        r"\1 \2\3",
        text,
    )
    text = re.sub(
        r"#\s*([a-z0-9]+)\s*-\s*([a-z][a-z0-9]{0,2})(?=[^a-z0-9]|$)",
        r" unit \1\2 ",
        text,
    )
    text = re.sub(r"-+", " ", text)
    text = re.sub(r"#\s*(?=[a-z0-9])", " unit ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _native_image_filename_address_claims(
    value: Any,
    *,
    strip_extension: bool,
) -> Tuple[List[Tuple[str, str, str, Optional[str], str, str, str]], bool]:
    """Return complete normalized claims plus an incomplete-claim sentinel."""
    normalized = _normalize_native_image_address_text(
        value,
        strip_extension=strip_extension,
    )
    if normalized is None:
        return [], True
    claims = []
    claimed_spans = []
    for match in _NATIVE_IMAGE_ADDRESS_RE.finditer(normalized):
        street_tokens = [
            _NATIVE_IMAGE_DIRECTIONAL_ALIASES.get(token, token)
            for token in match.group("street").split()
        ]
        state_text = " ".join(match.group("state").split())
        state_code = _NATIVE_IMAGE_STATE_ALIASES.get(state_text)
        suffix = _NATIVE_IMAGE_STREET_SUFFIX_ALIASES.get(match.group("suffix"))
        if not street_tokens or not state_code or not suffix:
            continue
        claims.append(
            (
                match.group("number"),
                " ".join(street_tokens),
                suffix,
                match.group("unit") or None,
                " ".join(match.group("city").split()),
                state_code,
                match.group("zip")[:5],
            )
        )
        claimed_spans.append(match.span())

    residue = list(normalized)
    for start, end in claimed_spans:
        residue[start:end] = " " * (end - start)
    unclaimed = "".join(residue)
    has_incomplete_claim = bool(
        _NATIVE_IMAGE_PARTIAL_STREET_RE.search(unclaimed)
        or _NATIVE_IMAGE_PARTIAL_STATE_ZIP_RE.search(unclaimed)
        or _NATIVE_IMAGE_PARTIAL_NUMBER_STREET_TOKEN_RE.search(unclaimed)
        or _NATIVE_IMAGE_PARTIAL_STREET_WITHOUT_NUMBER_RE.search(unclaimed)
        or _NATIVE_IMAGE_UNCLAIMED_ALPHANUMERIC_NUMBER_RE.search(unclaimed)
    )
    return claims, has_incomplete_claim


def _native_image_target_address_tuple(
    target_property_hint: Any,
) -> Optional[Tuple[str, str, str, Optional[str], str, str, str]]:
    claims, has_incomplete_claim = _native_image_filename_address_claims(
        target_property_hint,
        strip_extension=False,
    )
    if has_incomplete_claim or len(claims) != 1:
        return None
    return claims[0]


def classify_native_image_filename_binding(
    raw_filename: str,
    *,
    target_property_hint: str,
) -> Dict[str, str]:
    """Return only target/safe-method or a stable generic failure code."""
    target_tuple = _native_image_target_address_tuple(target_property_hint)
    if target_tuple is None:
        return {"failure_code": "image_attachment_unbound_property"}

    claims, has_incomplete_claim = _native_image_filename_address_claims(
        raw_filename,
        strip_extension=True,
    )
    distinct_claims = set(claims)
    errors = set()
    if len(distinct_claims) > 1:
        errors.add("image_attachment_mixed_property")
    if not claims or has_incomplete_claim:
        errors.add("image_attachment_unbound_property")
    if len(distinct_claims) == 1 and next(iter(distinct_claims)) != target_tuple:
        errors.add("image_attachment_wrong_property")

    failure = _select_native_image_failure(errors)
    if failure:
        return {"failure_code": failure}
    return {
        "property_binding": "target",
        "binding_method": "structured_filename_address",
    }


def _native_image_base64_decoded_size(value: Any) -> int:
    """Project decoded size with constant-space length and suffix checks."""
    if not isinstance(value, (str, bytes, bytearray)):
        raise ValueError("invalid base64")

    encoded_length = len(value)
    if encoded_length % 4:
        raise ValueError("invalid base64")

    double_padding = "==" if isinstance(value, str) else b"=="
    single_padding = "=" if isinstance(value, str) else b"="
    if value.endswith(double_padding):
        padding = 2
    elif value.endswith(single_padding):
        padding = 1
    else:
        padding = 0
    return (encoded_length // 4) * 3 - padding


def _strict_native_image_base64_decode(value: Any, decoded_size: int) -> bytes:
    """Strictly decode only after source and aggregate bounds have passed."""
    if isinstance(value, str):
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid base64") from exc
    elif isinstance(value, bytes):
        encoded = value
    elif isinstance(value, bytearray):
        encoded = bytes(value)
    else:
        raise ValueError("invalid base64")

    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("invalid base64") from exc
    if len(content) != decoded_size:
        raise ValueError("invalid base64")
    return content


def _native_image_magic_format(content: bytes) -> Optional[str]:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if content.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    return None


def _inspect_native_image_header(content: bytes) -> Tuple[str, int, int]:
    image_format = _native_image_magic_format(content)
    if image_format == "PNG":
        if len(content) < 24 or content[12:16] != b"IHDR":
            raise ValueError("invalid PNG header")
        width = int.from_bytes(content[16:20], "big")
        height = int.from_bytes(content[20:24], "big")
        return image_format, width, height

    if image_format == "JPEG":
        offset = 2
        while offset < len(content):
            while offset < len(content) and content[offset] != 0xFF:
                offset += 1
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                break

            marker = content[offset]
            offset += 1
            if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(content):
                break
            segment_length = int.from_bytes(content[offset:offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(content):
                break
            if marker in _NATIVE_IMAGE_JPEG_SOF_MARKERS:
                if segment_length < 7:
                    break
                height = int.from_bytes(content[offset + 3:offset + 5], "big")
                width = int.from_bytes(content[offset + 5:offset + 7], "big")
                return image_format, width, height
            if marker == 0xDA:
                break
            offset += segment_length

    raise ValueError("invalid image header")


def _inspect_native_image_pillow_format(content: bytes) -> Tuple[str, int, int]:
    if not HAS_PILLOW:
        raise RuntimeError("Pillow unavailable")
    with Image.open(io.BytesIO(content)) as image:
        width, height = image.size
        return str(image.format or "").upper(), int(width), int(height)


def _verify_native_image(content: bytes) -> None:
    if not HAS_PILLOW:
        raise RuntimeError("Pillow unavailable")
    with Image.open(io.BytesIO(content)) as image:
        image.verify()


def _normalize_native_image(content: bytes) -> Tuple[bytes, int, int]:
    if not HAS_PILLOW:
        raise RuntimeError("Pillow unavailable")

    with Image.open(io.BytesIO(content)) as source:
        oriented = ImageOps.exif_transpose(source)
        oriented.load()
        if max(oriented.size) > NATIVE_IMAGE_MAX_EDGE:
            oriented.thumbnail(
                (NATIVE_IMAGE_MAX_EDGE, NATIVE_IMAGE_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )

        rgba = oriented.convert("RGBA")
        alpha_minimum, _ = rgba.getchannel("A").getextrema()
        normalized_mode = "RGBA" if alpha_minimum < 255 else "RGB"
        normalized_pixels = (
            rgba if normalized_mode == "RGBA" else oriented.convert("RGB")
        )
        pixel_only = Image.frombytes(
            normalized_mode,
            normalized_pixels.size,
            normalized_pixels.tobytes(),
        )

    output = io.BytesIO()
    pixel_only.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    normalized_bytes = output.getvalue()
    width, height = pixel_only.size
    pixel_only.close()
    return normalized_bytes, int(width), int(height)


def _native_image_candidate(attachment: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(attachment, dict):
        return None
    if attachment.get("@odata.type") != NATIVE_IMAGE_FILE_ATTACHMENT_TYPE:
        return None
    if attachment.get("isInline") is not False:
        return None

    raw_name = attachment.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    extension = os.path.splitext(name)[1].lower()
    raw_content_type = attachment.get("contentType")
    content_type = (
        raw_content_type.strip().lower()
        if isinstance(raw_content_type, str)
        else ""
    )
    if (
        extension not in NATIVE_IMAGE_EXTENSION_MIME_TYPES
        and content_type not in NATIVE_IMAGE_MIME_FORMATS
    ):
        return None

    return {
        "_raw_filename": name,
        "extension": extension,
        "content_type": content_type,
        "content_bytes": attachment.get("contentBytes"),
    }


def validate_and_normalize_native_image_content_batch(
    attachments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return binding-neutral normalized native-image content or one failure."""
    candidates = [
        candidate
        for candidate in (
            _native_image_candidate(attachment)
            for attachment in (attachments or [])
        )
        if candidate is not None
    ]

    if len(candidates) > NATIVE_IMAGE_MAX_COUNT:
        return _native_image_failure("image_attachment_too_many")

    errors = set()
    decoded_sizes: List[int] = []
    prepared = []
    for candidate in candidates:
        expected_mime = NATIVE_IMAGE_EXTENSION_MIME_TYPES.get(
            candidate["extension"]
        )
        types_match = bool(
            expected_mime and expected_mime == candidate["content_type"]
        )
        if not types_match:
            errors.add("image_attachment_type_mismatch")

        try:
            decoded_size = _native_image_base64_decoded_size(
                candidate["content_bytes"]
            )
        except ValueError:
            errors.add("image_attachment_invalid_base64")
            decoded_size = None

        if decoded_size is not None:
            decoded_sizes.append(decoded_size)
        prepared.append(
            {
                **candidate,
                "expected_mime": expected_mime,
                "types_match": types_match,
                "decoded_size": decoded_size,
            }
        )

    size_failure = _native_image_size_failure(decoded_sizes, [])
    if size_failure:
        return _native_image_failure(size_failure)

    decoded = []
    for candidate in prepared:
        if candidate["decoded_size"] is None:
            continue
        try:
            content = _strict_native_image_base64_decode(
                candidate["content_bytes"],
                candidate["decoded_size"],
            )
        except ValueError:
            errors.add("image_attachment_invalid_base64")
            continue

        expected_format = NATIVE_IMAGE_MIME_FORMATS.get(
            candidate["expected_mime"]
        ) or NATIVE_IMAGE_MIME_FORMATS.get(candidate["content_type"])
        magic_format = _native_image_magic_format(content)
        if magic_format is None:
            errors.add("image_attachment_bad_magic")
            continue
        magic_matches_expected = magic_format == expected_format
        if candidate["types_match"] and not magic_matches_expected:
            errors.add("image_attachment_bad_magic")
        decoded.append(
            {
                **candidate,
                "content": content,
                "expected_format": expected_format,
                "magic_matches_expected": magic_matches_expected,
            }
        )

    pixel_counts: List[int] = []
    inspected = []
    for candidate in decoded:
        try:
            image_format, width, height = _inspect_native_image_header(
                candidate["content"]
            )
        except Exception:
            errors.add("image_attachment_decode_failed")
            continue
        if width <= 0 or height <= 0:
            errors.add("image_attachment_decode_failed")
            continue
        if (
            candidate["magic_matches_expected"]
            and image_format != candidate["expected_format"]
        ):
            errors.add("image_attachment_type_mismatch")
        pixel_counts.append(width * height)
        inspected.append(
            {
                **candidate,
                "header_width": width,
                "header_height": height,
            }
        )

    size_failure = _native_image_size_failure(decoded_sizes, pixel_counts)
    if size_failure:
        return _native_image_failure(size_failure)

    pillow_pixel_counts: List[int] = []
    for candidate in inspected:
        try:
            (
                pillow_format,
                pillow_width,
                pillow_height,
            ) = _inspect_native_image_pillow_format(
                candidate["content"],
            )
        except Exception:
            errors.add("image_attachment_decode_failed")
            continue
        if pillow_width > 0 and pillow_height > 0:
            pillow_pixel_counts.append(pillow_width * pillow_height)
        else:
            errors.add("image_attachment_decode_failed")
        if (
            pillow_width != candidate["header_width"]
            or pillow_height != candidate["header_height"]
        ):
            errors.add("image_attachment_too_large")
        if (
            candidate["magic_matches_expected"]
            and pillow_format != candidate["expected_format"]
        ):
            errors.add("image_attachment_type_mismatch")

    size_failure = _native_image_size_failure(decoded_sizes, pillow_pixel_counts)
    if size_failure:
        errors.add(size_failure)
    first_failure = _select_native_image_failure(errors)
    if first_failure:
        return _native_image_failure(first_failure)

    for candidate in inspected:
        try:
            _verify_native_image(candidate["content"])
        except Exception:
            errors.add("image_attachment_decode_failed")

    first_failure = _select_native_image_failure(errors)
    if first_failure:
        return _native_image_failure(first_failure)

    assets = []
    for candidate in inspected:
        try:
            normalized_data, width, height = _normalize_native_image(
                candidate["content"]
            )
        except Exception:
            errors.add("image_attachment_decode_failed")
            continue
        assets.append(
            {
                "name": _NATIVE_IMAGE_GENERIC_NAME,
                "content_type": "image/png",
                "data": normalized_data,
                "width": width,
                "height": height,
                "source_bytes": len(candidate["content"]),
                "normalized_bytes": len(normalized_data),
                "normalized_sha256": hashlib.sha256(normalized_data).hexdigest(),
            }
        )

    first_failure = _select_native_image_failure(errors)
    if first_failure:
        return _native_image_failure(first_failure)
    return {"status": "accepted", "assets": assets}


def validate_and_normalize_native_image_attachments(
    attachments: List[Dict[str, Any]],
    *,
    target_property_hint: str,
) -> Dict[str, Any]:
    """Return fully bound normalized assets or one stable batch failure."""
    attachment_items = list(attachments or [])
    content_batch = validate_and_normalize_native_image_content_batch(
        attachment_items
    )
    if content_batch.get("status") != "accepted":
        return content_batch

    private_candidates = [
        candidate
        for candidate in (
            _native_image_candidate(attachment)
            for attachment in attachment_items
        )
        if candidate is not None
    ]
    if not private_candidates:
        return content_batch
    if len(private_candidates) != len(content_batch.get("assets") or []):
        return _native_image_failure("image_attachment_decode_failed")

    target_tuple = _native_image_target_address_tuple(target_property_hint)
    binding_errors = set()
    if target_tuple is None:
        binding_errors.add("image_attachment_unbound_property")

    distinct_claims = set()
    for candidate in private_candidates:
        claims, has_incomplete_claim = _native_image_filename_address_claims(
            candidate["_raw_filename"],
            strip_extension=True,
        )
        distinct_claims.update(claims)
        if not claims or has_incomplete_claim:
            binding_errors.add("image_attachment_unbound_property")

    if len(distinct_claims) > 1:
        binding_errors.add("image_attachment_mixed_property")
    elif (
        len(distinct_claims) == 1
        and target_tuple is not None
        and next(iter(distinct_claims)) != target_tuple
    ):
        binding_errors.add("image_attachment_wrong_property")

    binding_failure = _select_native_image_failure(binding_errors)
    if binding_failure:
        return _native_image_failure(binding_failure)

    assets = [
        {
            **asset,
            "property_binding": "target",
            "binding_method": "structured_filename_address",
        }
        for asset in content_batch["assets"]
    ]
    return {"status": "accepted", "assets": assets}


def build_native_image_manifest_entry(
    batch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project an accepted bound batch into the shared safe manifest shape."""
    if not isinstance(batch, dict) or batch.get("status") != "accepted":
        return None
    assets = batch.get("assets")
    if (
        not isinstance(assets, list)
        or not assets
        or len(assets) > NATIVE_IMAGE_MAX_COUNT
    ):
        return None

    validated_data = []
    image_meta = []
    aggregate_source_bytes = 0
    for asset in assets:
        if (
            type(asset) is not dict
            or len(asset) != len(_NATIVE_IMAGE_BOUND_ASSET_KEYS)
            or set(asset) != _NATIVE_IMAGE_BOUND_ASSET_KEYS
        ):
            return None
        if (
            asset.get("name") != _NATIVE_IMAGE_GENERIC_NAME
            or asset.get("property_binding") != "target"
            or asset.get("binding_method")
            != "structured_filename_address"
            or asset.get("content_type") != "image/png"
            or type(asset.get("data")) is not bytes
        ):
            return None

        data = asset["data"]
        width = asset["width"]
        height = asset["height"]
        source_bytes = asset["source_bytes"]
        normalized_bytes = asset["normalized_bytes"]
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or max(width, height) > NATIVE_IMAGE_MAX_EDGE
            or width * height > NATIVE_IMAGE_MAX_PIXELS
            or type(source_bytes) is not int
            or source_bytes <= 0
            or source_bytes > NATIVE_IMAGE_MAX_SOURCE_BYTES
            or type(normalized_bytes) is not int
            or normalized_bytes <= 0
            or normalized_bytes != len(data)
            or normalized_bytes > NATIVE_IMAGE_MAX_SOURCE_BYTES
            or type(asset.get("normalized_sha256")) is not str
            or hashlib.sha256(data).hexdigest()
            != asset.get("normalized_sha256")
        ):
            return None

        try:
            header_format, header_width, header_height = (
                _inspect_native_image_header(data)
            )
            pillow_format, pillow_width, pillow_height = (
                _inspect_native_image_pillow_format(data)
            )
        except Exception:
            return None
        if (
            header_format != "PNG"
            or pillow_format != "PNG"
            or header_width <= 0
            or header_height <= 0
            or max(header_width, header_height) > NATIVE_IMAGE_MAX_EDGE
            or header_width * header_height > NATIVE_IMAGE_MAX_PIXELS
            or (header_width, header_height) != (width, height)
            or (pillow_width, pillow_height) != (width, height)
        ):
            return None
        try:
            _verify_native_image(data)
            canonical_data, canonical_width, canonical_height = (
                _normalize_native_image(data)
            )
        except Exception:
            return None
        if (
            (canonical_width, canonical_height) != (width, height)
            or canonical_data != data
        ):
            return None

        aggregate_source_bytes += source_bytes
        if aggregate_source_bytes > NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES:
            return None
        validated_data.append(data)
        image_meta.append(
            {
                "content_type": "image/png",
                "width": width,
                "height": height,
                "source_bytes": source_bytes,
                "normalized_bytes": normalized_bytes,
                "normalized_sha256": asset["normalized_sha256"],
            }
        )

    return {
        "name": _NATIVE_IMAGE_GENERIC_NAME,
        "text": "",
        "images": [
            base64.b64encode(data).decode("ascii")
            for data in validated_data
        ],
        "method": "native_image_normalized",
        "source_type": "native_image",
        "property_binding": "target",
        "binding_method": "structured_filename_address",
        "image_meta": image_meta,
    }


def build_native_image_failure_manifest_entry(
    batch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project one quarantined native batch into a generic retry marker."""
    if not isinstance(batch, dict) or batch.get("status") != "quarantined":
        return None
    failure = batch.get("failure")
    code = failure.get("code") if isinstance(failure, dict) else None
    if code not in _NATIVE_IMAGE_FAILURE_PRECEDENCE:
        return None
    return {
        "name": _NATIVE_IMAGE_GENERIC_NAME,
        "text": "",
        "images": [],
        "method": "native_image_quarantined",
        "source_type": "native_image",
        "extraction_failed": True,
        "native_image_failure": True,
        "failure_code": code,
    }


@dataclass(frozen=True)
class _NativeImageMetadataSnapshot:
    content_type: str
    width: int
    height: int
    source_bytes: int
    normalized_bytes: int
    normalized_sha256: str

    def safe_projection(self) -> Dict[str, Any]:
        safe_values = {
            "content_type": self.content_type,
            "width": self.width,
            "height": self.height,
            "source_bytes": self.source_bytes,
            "normalized_bytes": self.normalized_bytes,
            "normalized_sha256": self.normalized_sha256,
        }
        return {
            key: safe_values[key]
            for key in _NATIVE_IMAGE_SAFE_META_KEYS
        }


@dataclass(frozen=True)
class _NativeImagePreflightAsset:
    encoded_image: str
    metadata: _NativeImageMetadataSnapshot
    projected_decoded_size: int


@dataclass(frozen=True)
class _NativeImageManifestPreflight:
    images: Tuple[_NativeImagePreflightAsset, ...]
    source_bytes: int
    normalized_bytes: int


@dataclass(frozen=True)
class _PreparedNativeImageManifest:
    """Validated immutable transport plus fresh safe audit projections."""

    images: Tuple[str, ...]
    image_meta: Tuple[_NativeImageMetadataSnapshot, ...]

    def safe_projection(self) -> Dict[str, Any]:
        safe_values = {
            "name": _NATIVE_IMAGE_GENERIC_NAME,
            "text": "",
            "method": "native_image_normalized",
            "source_type": "native_image",
            "property_binding": "target",
            "binding_method": "structured_filename_address",
            "image_meta": [
                metadata.safe_projection()
                for metadata in self.image_meta
            ],
        }
        return {
            key: safe_values[key]
            for key in _NATIVE_IMAGE_SAFE_MANIFEST_KEYS
        }


def _preflight_native_image_manifest_metadata(
    manifest: Any,
) -> Optional[_NativeImageManifestPreflight]:
    """Validate bounded manifest metadata without decoding image payloads."""
    if type(manifest) is not dict:
        return None

    exact_markers = (
        ("name", _NATIVE_IMAGE_GENERIC_NAME),
        ("text", ""),
        ("method", "native_image_normalized"),
        ("source_type", "native_image"),
        ("property_binding", "target"),
        ("binding_method", "structured_filename_address"),
    )
    if any(
        type(manifest.get(key)) is not str
        or manifest.get(key) != expected
        for key, expected in exact_markers
    ):
        return None

    encoded_images = manifest.get("images")
    image_meta = manifest.get("image_meta")
    if (
        type(encoded_images) is not list
        or type(image_meta) is not list
        or not encoded_images
        or len(encoded_images) > NATIVE_IMAGE_MAX_COUNT
        or len(encoded_images) != len(image_meta)
    ):
        return None

    # Snapshot only bounded immutable scalars. No source mapping or mutable list
    # reference survives this request-wide first pass.
    preflight_images = []
    aggregate_source_bytes = 0
    aggregate_normalized_bytes = 0
    for encoded_image, metadata in zip(encoded_images, image_meta):
        if type(encoded_image) is not str or type(metadata) is not dict:
            return None

        content_type = metadata.get("content_type")
        width = metadata.get("width")
        height = metadata.get("height")
        source_bytes = metadata.get("source_bytes")
        normalized_bytes = metadata.get("normalized_bytes")
        normalized_sha256 = metadata.get("normalized_sha256")
        if (
            type(content_type) is not str
            or content_type != "image/png"
            or type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or max(width, height) > NATIVE_IMAGE_MAX_EDGE
            or width * height > NATIVE_IMAGE_MAX_PIXELS
            or type(source_bytes) is not int
            or source_bytes <= 0
            or source_bytes > NATIVE_IMAGE_MAX_SOURCE_BYTES
            or type(normalized_bytes) is not int
            or normalized_bytes <= 0
            or normalized_bytes > NATIVE_IMAGE_MAX_SOURCE_BYTES
            or type(normalized_sha256) is not str
        ):
            return None

        try:
            projected_decoded_size = _native_image_base64_decoded_size(
                encoded_image
            )
        except ValueError:
            return None
        if (
            projected_decoded_size != normalized_bytes
            or projected_decoded_size > NATIVE_IMAGE_MAX_SOURCE_BYTES
        ):
            return None

        aggregate_source_bytes += source_bytes
        aggregate_normalized_bytes += projected_decoded_size
        preflight_images.append(_NativeImagePreflightAsset(
            encoded_image=encoded_image,
            metadata=_NativeImageMetadataSnapshot(
                content_type=content_type,
                width=width,
                height=height,
                source_bytes=source_bytes,
                normalized_bytes=normalized_bytes,
                normalized_sha256=normalized_sha256,
            ),
            projected_decoded_size=projected_decoded_size,
        ))

    return _NativeImageManifestPreflight(
        images=tuple(preflight_images),
        source_bytes=aggregate_source_bytes,
        normalized_bytes=aggregate_normalized_bytes,
    )


def _project_preflighted_native_image_manifest(
    preflight: _NativeImageManifestPreflight,
) -> Optional[_PreparedNativeImageManifest]:
    """Fully validate one manifest after all request-wide bounds pass."""
    safe_meta = []
    transport_images = []
    for asset in preflight.images:
        encoded_image = asset.encoded_image
        metadata = asset.metadata
        projected_decoded_size = asset.projected_decoded_size
        try:
            normalized_data = _strict_native_image_base64_decode(
                encoded_image,
                projected_decoded_size,
            )
        except ValueError:
            return None
        width = metadata.width
        height = metadata.height
        normalized_bytes = metadata.normalized_bytes
        normalized_sha256 = metadata.normalized_sha256
        if (
            len(normalized_data) != normalized_bytes
            or hashlib.sha256(normalized_data).hexdigest()
            != normalized_sha256
        ):
            return None

        # Inspect both independent dimension sources before any full decode or
        # canonical re-normalization. Forged declared dimensions therefore fail
        # closed without allowing an oversized image into expensive pixel work.
        try:
            header_format, header_width, header_height = (
                _inspect_native_image_header(normalized_data)
            )
            pillow_format, pillow_width, pillow_height = (
                _inspect_native_image_pillow_format(normalized_data)
            )
        except Exception:
            return None
        if (
            header_format != "PNG"
            or pillow_format != "PNG"
            or header_width <= 0
            or header_height <= 0
            or pillow_width <= 0
            or pillow_height <= 0
            or max(header_width, header_height) > NATIVE_IMAGE_MAX_EDGE
            or header_width * header_height > NATIVE_IMAGE_MAX_PIXELS
            or max(pillow_width, pillow_height) > NATIVE_IMAGE_MAX_EDGE
            or pillow_width * pillow_height > NATIVE_IMAGE_MAX_PIXELS
            or (header_width, header_height) != (pillow_width, pillow_height)
            or (header_width, header_height) != (width, height)
        ):
            return None

        try:
            _verify_native_image(normalized_data)
            canonical_data, canonical_width, canonical_height = (
                _normalize_native_image(normalized_data)
            )
        except Exception:
            return None
        if (
            canonical_data != normalized_data
            or (canonical_width, canonical_height) != (width, height)
        ):
            return None

        transport_images.append(encoded_image)
        safe_meta.append(metadata)

    return _PreparedNativeImageManifest(
        images=tuple(transport_images),
        image_meta=tuple(safe_meta),
    )


def _prepare_safe_native_image_manifests(
    manifests: List[Dict[str, Any]],
) -> Optional[Tuple[_PreparedNativeImageManifest, ...]]:
    """Seal a request batch only after metadata-only global preflight."""
    if type(manifests) is not list:
        return None

    preflighted = []
    aggregate_asset_count = 0
    aggregate_source_bytes = 0
    aggregate_normalized_bytes = 0
    for manifest in manifests:
        preflight = _preflight_native_image_manifest_metadata(manifest)
        if preflight is None:
            return None
        aggregate_asset_count += len(preflight.images)
        aggregate_source_bytes += preflight.source_bytes
        aggregate_normalized_bytes += preflight.normalized_bytes
        if (
            aggregate_asset_count > NATIVE_IMAGE_MAX_COUNT
            or aggregate_source_bytes > NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES
            or aggregate_normalized_bytes
            > NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES
        ):
            return None
        preflighted.append(preflight)

    prepared = []
    for preflight in preflighted:
        sealed_manifest = _project_preflighted_native_image_manifest(preflight)
        if sealed_manifest is None:
            return None
        prepared.append(sealed_manifest)
    return tuple(prepared)


def project_safe_native_image_manifests(
    manifests: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Validate a request batch and return fresh safe audit projections."""
    prepared = _prepare_safe_native_image_manifests(manifests)
    if prepared is None:
        return None
    return [manifest.safe_projection() for manifest in prepared]


def project_safe_native_image_manifest(
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Validate one canonical native-image entry and safely project it."""
    projections = project_safe_native_image_manifests([manifest])
    if projections is None or len(projections) != 1:
        return None
    return projections[0]


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

    if extracted_text and len(_pdf_substantive_text_for_threshold(extracted_text)) > 100:
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


def fetch_message_attachment_snapshot(
    headers: Dict[str, str],
    graph_msg_id: str,
) -> List[Dict[str, Any]]:
    """Fetch one bounded, ordered, paginated Graph attachment snapshot."""
    base = "https://graph.microsoft.com/v1.0"
    url = f"{base}/me/messages/{graph_msg_id}/attachments"
    attachments: List[Dict[str, Any]] = []
    page_count = 0

    while url:
        page_count += 1
        if page_count > GRAPH_ATTACHMENT_SNAPSHOT_MAX_PAGES:
            raise requests.exceptions.RequestException(
                "Graph attachment snapshot exceeded the page limit"
            )
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json() or {}
        values = payload.get("value", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            raise requests.exceptions.RequestException(
                "Graph attachment snapshot returned an invalid page"
            )
        attachments.extend(values)
        if len(attachments) > GRAPH_ATTACHMENT_SNAPSHOT_MAX_ITEMS:
            raise requests.exceptions.RequestException(
                "Graph attachment snapshot exceeded the item limit"
            )

        next_link = payload.get("@odata.nextLink") if isinstance(payload, dict) else None
        if next_link is None:
            url = ""
        elif (
            isinstance(next_link, str)
            and next_link.startswith("https://graph.microsoft.com/")
        ):
            url = next_link
        else:
            raise requests.exceptions.RequestException(
                "Graph attachment snapshot returned an invalid next link"
            )

    return attachments


class _PdfAttachmentList(list):
    """PDF projection that retains its originating Graph snapshot."""

    def __init__(
        self,
        values: List[Dict[str, Any]],
        attachment_snapshot: List[Dict[str, Any]],
    ) -> None:
        super().__init__(values)
        self.attachment_snapshot = attachment_snapshot


def fetch_pdf_attachments(
    headers: Dict[str, str],
    graph_msg_id: str,
    *,
    attachment_snapshot: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
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
    attachments = (
        fetch_message_attachment_snapshot(headers, graph_msg_id)
        if attachment_snapshot is None
        else attachment_snapshot
    )
    pdf_attachments = []

    for position, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            continue
        # A one-sided native image type claim is owned by the native validator.
        # It must quarantine there, never fall through and get reprocessed as a
        # PDF merely because its other type field says application/pdf.
        if (
            _native_image_candidate(attachment) is None
            and attachment.get("contentType", "").lower() == "application/pdf"
        ):
            name = attachment.get("name", "document.pdf")
            content_bytes = base64.b64decode(attachment.get("contentBytes", ""))
            pdf_attachments.append({
                "name": name,
                "bytes": content_bytes,
                "_snapshot_index": position,
            })

    print(f"📎 Found {len(pdf_attachments)} PDF attachment(s)")
    return _PdfAttachmentList(pdf_attachments, attachments)

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


def host_first_native_image_manifest_asset(
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Host only the first sealed native image and return allowlisted fields."""
    prepared = _prepare_safe_native_image_manifests([manifest])
    if prepared is None or len(prepared) != 1 or not prepared[0].images:
        return None

    sealed = prepared[0]
    encoded_image = sealed.images[0]
    metadata = sealed.image_meta[0]
    try:
        normalized_data = _strict_native_image_base64_decode(
            encoded_image,
            metadata.normalized_bytes,
        )
    except ValueError:
        return None

    upload_name = (
        f"broker-property-image-{metadata.normalized_sha256[:16]}.png"
    )
    uploaded = upload_property_image_to_drive(upload_name, normalized_data)
    if not isinstance(uploaded, dict):
        return None
    image_url = uploaded.get("url")
    if not isinstance(image_url, str):
        return None
    image_url = image_url.strip()
    try:
        parsed_url = urlparse(image_url)
    except Exception:
        return None
    if (
        parsed_url.scheme.lower() != "https"
        or parsed_url.netloc.lower() != "drive.google.com"
    ):
        return None

    return {
        "property_image_url": image_url,
        "property_image_source": "Broker native property image",
        "property_image_source_type": "native_image",
        "property_image_meta": {
            "strategy": "native_image_normalized_v1",
            "selectionReason": "prevalidated target native image",
            "contentType": "image/png",
            "byteCount": metadata.normalized_bytes,
            "sha256": metadata.normalized_sha256,
            "width": metadata.width,
            "height": metadata.height,
        },
    }


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


def _linked_asset_stub_entry(
    *,
    name: str,
    source_url: str,
    method: str,
    source_type: str,
    error: str,
    **flags: bool,
) -> Dict[str, Any]:
    """Build a non-content manifest entry (manual-review or failed-download).

    These entries carry no extracted text/images/drive_link — they exist only
    to keep an un-resolvable broker link VISIBLE downstream rather than letting
    it be silently dropped (a dropped link is indistinguishable from 'no
    assets' and would let the message be marked processed with payload lost).
    Callers pass the distinguishing flag(s) (e.g. requires_manual_review=True
    or download_failed=True) as keyword arguments.
    """
    entry: Dict[str, Any] = {
        "name": name,
        "text": "",
        "images": [],
        "method": method,
        "source_url": source_url,
        "source_type": source_type,
        "drive_link": None,
        "error": error,
    }
    entry.update(flags)
    return entry


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
        manual_review_reasons: List[str] = []
        candidate = build_download_candidate(
            source_url,
            filename_hint=filename_hint,
            target_property_hint=target_property_hint,
            manual_review_reasons=manual_review_reasons,
        )
        if not candidate:
            # None with a recorded reason == an address-bearing link we could
            # not verify without target context. Do NOT silently drop it (a
            # dropped link is indistinguishable from 'no assets' and lets the
            # message be marked processed with the broker's payload lost) —
            # surface it as a manual-review entry. A None with NO reason is a
            # confident drop (blocked/unsupported host or a hint-confirmed
            # wrong-property flyer) and stays dropped.
            if manual_review_reasons:
                name = _filename_from_asset_url(source_url, filename_hint or "broker flyer.pdf")
                print(f"⚠️ Broker link needs manual review (unverifiable property address, no target context): {source_url}")
                processed.append(_linked_asset_stub_entry(
                    name=name,
                    source_url=source_url,
                    method="manual_review_required",
                    source_type="broker_unverified_property_link",
                    error=manual_review_reasons[0],
                    requires_manual_review=True,
                ))
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
            processed.append(_linked_asset_stub_entry(
                name=name,
                source_url=source_url,
                method="manual_review_required",
                source_type=candidate.get("sourceType") or "broker_file_share_link",
                error="Broker file-share link could not be auto-downloaded; needs manual review",
                requires_manual_review=True,
            ))
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
            processed.append(_linked_asset_stub_entry(
                name=name,
                source_url=source_url,
                method="failed",
                source_type=candidate.get("sourceType") or "",
                error=str(e),
                download_failed=True,
            ))
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
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(content)
            tmp_file.flush()

            with open(tmp_path, "rb") as f:
                file_response = client.files.create(
                    file=f,
                    purpose="user_data"
                )

            file_id = file_response.id
            print(f"📤 Uploaded to OpenAI: {filename} -> {file_id}")
            return file_id

    except Exception as e:
        print(f"❌ Failed to upload PDF to OpenAI: {e}")
        raise
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _process_pdf_attachment_batch(
    attachments: List[Dict[str, Any]],
) -> List[Tuple[int, Dict[str, Any]]]:
    """Process already-fetched PDFs while retaining their snapshot positions."""
    processed: List[Tuple[int, Dict[str, Any]]] = []
    for fallback_position, attachment in enumerate(attachments or []):
        name = attachment.get("name", "document.pdf")
        content = attachment.get("bytes", b"")
        snapshot_position = attachment.get("_snapshot_index")
        if type(snapshot_position) is not int or snapshot_position < 0:
            snapshot_position = fallback_position

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
            processed.append((snapshot_position, {
                "name": name,
                "text": "",
                "images": result.get("images") or [],
                "method": "failed_extraction",
                "file_id": None,
                "id": None,
                "drive_link": None,
                "extraction_failed": True,
                "error": "PDF text extraction and OpenAI upload both failed",
            }))
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

        processed.append((snapshot_position, result))

    return processed


def fetch_and_process_pdfs(
    headers: Dict[str, str],
    graph_msg_id: str,
    *,
    target_property_hint: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Process current-message PDFs and, when targeted, native images.

    Legacy direct callers omit ``target_property_hint`` and retain the existing
    PDF-only path. Processing supplies even an incomplete/empty row value so one
    paginated Graph snapshot owns both PDF and native-image routing.
    """
    pdf_attachments = fetch_pdf_attachments(headers, graph_msg_id)
    if target_property_hint is None:
        return [
            entry
            for _position, entry in _process_pdf_attachment_batch(
                pdf_attachments
            )
        ]

    # ``fetch_pdf_attachments`` is an established test and integration seam.
    # Production returns the exact snapshot-bearing list above; older controlled
    # callers may return a plain PDF list, which safely represents no native
    # candidates without triggering an unmocked second Graph request.
    attachment_snapshot = getattr(
        pdf_attachments,
        "attachment_snapshot",
        [],
    )
    if not isinstance(attachment_snapshot, list):
        raise ValueError("PDF attachment snapshot projection failed closed")
    positioned_entries = [
        (position, 1, entry)
        for position, entry in _process_pdf_attachment_batch(pdf_attachments)
    ]

    native_positions = [
        position
        for position, attachment in enumerate(attachment_snapshot)
        if _native_image_candidate(attachment) is not None
    ]
    native_batch = validate_and_normalize_native_image_attachments(
        attachment_snapshot,
        target_property_hint=target_property_hint,
    )
    if native_positions:
        if native_batch.get("status") == "accepted":
            native_assets = native_batch.get("assets")
            if (
                not isinstance(native_assets, list)
                or len(native_assets) != len(native_positions)
            ):
                raise ValueError("Native image snapshot projection failed closed")
            for position, native_asset in zip(native_positions, native_assets):
                native_entry = build_native_image_manifest_entry({
                    "status": "accepted",
                    "assets": [native_asset],
                })
                if native_entry is None:
                    raise ValueError("Native image manifest projection failed closed")
                positioned_entries.append((position, 0, native_entry))
        else:
            native_entry = build_native_image_failure_manifest_entry(native_batch)
            if native_entry is None:
                raise ValueError("Native image manifest projection failed closed")
            positioned_entries.append((native_positions[0], 0, native_entry))

    positioned_entries.sort(key=lambda item: (item[0], item[1]))
    return [entry for _position, _priority, entry in positioned_entries]
