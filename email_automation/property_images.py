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
# Common CRE file-share hosts. Broker links on these hosts carry no direct file
# extension and are not Drive/Dropbox *direct* files, so none of the extension /
# drive-id branches below match them. A link-ONLY broker email on one of these
# hosts is real broker payload ("everything's in this SharePoint/Box/WeTransfer
# link"). Returning None silently drops that payload and lets the pipeline mark
# the message processed with the broker's data lost. We cannot resolve the
# underlying file deterministically, so we FLAG the link for manual review
# instead of dropping it — fail closed / stay visible.
FILE_SHARE_HOSTS = (
    "sharepoint.com",
    "onedrive.live.com",
    "onedrive.com",
    "1drv.ms",
    "box.com",
    "boxcloud.com",
    "wetransfer.com",
    "we.tl",
)
DIRECT_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
)
DIRECT_PDF_EXTENSIONS = (
    ".pdf",
)
SAFE_DIRECT_IMAGE_HOSTS = (
    "drive.google.com",
    "googleusercontent.com",
    "dropbox.com",
    "dropboxusercontent.com",
)

# --- Wrong-property address guard -------------------------------------------
# A forwarded flyer for a DIFFERENT address must not silently become the row's
# flyer/preview source (its preview would populate property_image_url on the
# current row). Deterministic address pattern: street-number token + 1..3
# street-name tokens + street-suffix token (e.g. "123-Main-St", "4402 Rex Rd",
# "1419 Atlantis Drive").
STREET_SUFFIX_TOKENS = frozenset((
    "st", "street", "ave", "avenue", "av", "rd", "road", "blvd", "boulevard",
    "dr", "drive", "ln", "lane", "ct", "court", "way", "pl", "place",
    "hwy", "highway", "fwy", "freeway", "pkwy", "parkway", "ter", "terrace",
    "cir", "circle", "sq", "square", "trl", "trail", "loop", "plaza", "pike",
    "row", "aly", "alley", "bnd", "bend", "xing", "crossing",
))
# Property-agnostic tokens that legitimately ride along with an address in a
# broker filename ("4402 Rex Rd Flyer.pdf"). Anything alphabetic OUTSIDE this
# vocabulary next to an address is a potentially property-identifying claim
# (another city / cross street / marker) that we cannot verify without target
# context — fail closed in that case.
ADDRESS_NEUTRAL_TOKENS = frozenset((
    # document descriptors
    "flyer", "flyers", "brochure", "brochures", "om", "offering", "memorandum",
    "floorplan", "floorplans", "floor", "plan", "plans", "site", "sitemap",
    "survey", "marketing", "package", "packet", "pkg", "deck", "listing",
    "lease", "sublease", "sale", "photos", "photo", "pics", "images", "image",
    "info", "details", "detail", "spec", "specs", "sheet", "onepager",
    "one", "pager", "final", "updated", "revised", "new", "draft", "copy",
    # intra-property qualifiers (same property, not a different one)
    "suite", "ste", "unit", "bldg", "building", "space",
    # file extensions
    "pdf", "jpg", "jpeg", "png", "webp", "gif",
))

# Geographic qualifiers that merely LOCATE the one street address in a filename
# ("4402 Rex Rd Webster TX Flyer.pdf") rather than assert a second, competing
# property. Without target context the earlier heuristic treated the city/state
# as unverifiable extra identifying tokens and dropped a perfectly valid
# same-property flyer (CodeRabbit PR#15 regression). US state abbreviations +
# full names + compass directionals are recognized as geographic; the token
# immediately preceding a state token is treated as its city (the "City, ST"
# convention). A genuinely-different property still differs in its STREET
# address (captured as its own claim / non-geographic token), so relaxing the
# geographic tail does not weaken the wrong-property guard.
_US_STATE_ABBREVIATIONS = frozenset((
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
))
_US_STATE_NAMES = frozenset((
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon",
    "pennsylvania", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "wisconsin", "wyoming",
))
_COMPASS_DIRECTIONALS = frozenset((
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
))
GEOGRAPHIC_QUALIFIER_TOKENS = (
    _US_STATE_ABBREVIATIONS | _US_STATE_NAMES | _COMPASS_DIRECTIONALS
)


def _guard_tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _clean(text).lower()) if t]


def _address_claims_in_tokens(tokens: List[str]):
    """Scan one filename / path segment for street-address claims.

    Returns (claims, has_unverified_extra) where each claim is
    (street_number, street_name_tokens) and has_unverified_extra is True when
    the segment also carries alphabetic tokens that are neither part of the
    address nor in the neutral vocabulary (an unverifiable property claim).
    """
    claims = []
    consumed = set()
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token.isdigit() and 1 <= len(token) <= 6:
            for j in range(i + 1, min(i + 5, n)):
                lookahead = tokens[j]
                if lookahead.isdigit():
                    break
                if lookahead in STREET_SUFFIX_TOKENS and j > i + 1:
                    claims.append((token, tuple(tokens[i + 1:j])))
                    consumed.update(range(i, j + 1))
                    i = j
                    break
        i += 1
    # Geographic qualifiers (state abbrev/name, compass directional) and the
    # city token that conventionally precedes a state ("City, ST") merely locate
    # the one street address; they are not a competing property claim.
    geographic = set()
    for k, token in enumerate(tokens):
        is_state = token in _US_STATE_ABBREVIATIONS or token in _US_STATE_NAMES
        if token in GEOGRAPHIC_QUALIFIER_TOKENS:
            geographic.add(k)
        if is_state and k - 1 >= 0 and not tokens[k - 1].isdigit():
            # The immediately-preceding non-numeric token is the city name.
            geographic.add(k - 1)

    has_unverified_extra = bool(claims) and any(
        k not in consumed
        and k not in geographic
        and not tokens[k].isdigit()
        and tokens[k] not in ADDRESS_NEUTRAL_TOKENS
        for k in range(n)
    )
    return claims, has_unverified_extra


def _collect_address_claims(source_url: str, filename_hint: str):
    """Address claims from the filename hint and each URL path segment."""
    segments = []
    hint = _clean(filename_hint)
    if hint:
        segments.append(hint)
    try:
        path = urlparse(source_url).path
    except Exception:
        path = ""
    segments.extend(part for part in path.split("/") if part)

    claims = []
    unverified_extra = False
    for segment in segments:
        segment_claims, segment_extra = _address_claims_in_tokens(
            _guard_tokens(segment))
        if segment_claims:
            claims.extend(segment_claims)
            unverified_extra = unverified_extra or segment_extra
    return claims, unverified_extra


def _claim_matches_target(claim, target_tokens) -> bool:
    street_number, street_names = claim
    if street_number not in target_tokens:
        return False
    return all(name in target_tokens for name in street_names)


# Verdicts from the property-address guard.
GUARD_OK = "ok"
GUARD_REJECT_WRONG_PROPERTY = "reject_wrong_property"
GUARD_MANUAL_REVIEW = "manual_review"


def _property_address_guard_verdict(
    source_url: str,
    filename_hint: str,
    target_property_hint: str,
) -> str:
    """Classify a link/filename against the wrong-property address guard.

    - ``GUARD_OK`` — safe to build a download candidate.
    - ``GUARD_REJECT_WRONG_PROPERTY`` — a target hint IS present and the
      filename/URL names a clearly different street address. We are confident
      it is the wrong property: hard reject (drop).
    - ``GUARD_MANUAL_REVIEW`` — NO target hint and the address-bearing name
      also carries a non-neutral, non-geographic identifying token we cannot
      verify. We are NOT confident it is wrong, so it must be surfaced for
      manual review rather than silently dropped (no-silent-drop contract).

    A plain address-plus-descriptor name (e.g. "4402 Rex Rd Flyer.pdf") or one
    whose only extra tokens are geographic qualifiers ("Webster, TX") stays
    ``GUARD_OK`` — production does not always have target context at this
    boundary, and dropping every address-bearing flyer would silently lose
    correct-property payloads.
    """
    claims, unverified_extra = _collect_address_claims(source_url, filename_hint)
    if not claims:
        return GUARD_OK
    target = _clean(target_property_hint)
    if target:
        target_tokens = set(_guard_tokens(target))
        if any(_claim_matches_target(claim, target_tokens) for claim in claims):
            return GUARD_OK
        return GUARD_REJECT_WRONG_PROPERTY
    return GUARD_MANUAL_REVIEW if unverified_extra else GUARD_OK


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


def _looks_like_pdf_asset(source_url: str, filename_hint: str = "") -> bool:
    lower_hint = _clean(filename_hint).lower()
    if lower_hint.endswith(DIRECT_PDF_EXTENSIONS):
        return True
    try:
        lower_path = urlparse(source_url).path.lower()
    except Exception:
        lower_path = ""
    return lower_path.endswith(DIRECT_PDF_EXTENSIONS)


def is_blocked_listing_url(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in BLOCKED_LISTING_DOMAINS)


def _clean_host(url: str) -> str:
    """Robust host extraction. ``_host`` uses ``str.lstrip('www.')`` which strips
    a *character set* and mangles hosts like ``we.tl`` -> ``e.tl``; use a proper
    prefix strip here so short file-share hosts are matched correctly."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _is_file_share_host(url: str) -> bool:
    host = _clean_host(url)
    return any(_host_matches(host, domain) for domain in FILE_SHARE_HOSTS)


def _is_drive_folder_url(url: str) -> bool:
    """A Google Drive *folder* link (…/drive/folders/…) is not a single file, so
    ``_drive_file_id`` returns None and it would otherwise be silently dropped."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = _clean_host(url)
    return host.endswith("drive.google.com") and "/drive/folders/" in parsed.path


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


def build_download_candidate(
    url: str,
    filename_hint: str = "",
    target_property_hint: str = "",
    manual_review_reasons: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    source_url = _clean(url)
    if not source_url or is_blocked_listing_url(source_url):
        return None

    # Wrong-property guard (deterministic): a flyer whose filename/URL names a
    # street address that does not verifiably belong to the target property
    # must not become the row's flyer/preview source — its preview would
    # populate property_image_url on the wrong row.
    #   - Confident wrong property (hint present, address differs) -> hard drop.
    #   - Unverifiable (no hint, non-geographic extra token) -> return None but
    #     record a manual-review reason via ``manual_review_reasons`` so the
    #     caller can surface the link instead of silently losing the payload.
    verdict = _property_address_guard_verdict(
        source_url, filename_hint, target_property_hint
    )
    if verdict == GUARD_REJECT_WRONG_PROPERTY:
        return None
    if verdict == GUARD_MANUAL_REVIEW:
        if manual_review_reasons is not None:
            manual_review_reasons.append(
                "Address-bearing broker link could not be verified against a "
                "target property (no target context available); needs manual "
                f"review: {_clean(filename_hint) or source_url}"
            )
        return None

    file_name = _clean(filename_hint) or "broker attachment"
    host = _host(source_url)
    looks_like_image = _looks_like_image_asset(source_url, file_name)
    is_direct_image = looks_like_image or (
        _is_safe_direct_image_host(host) and host.endswith("googleusercontent.com")
    )
    is_direct_pdf = _looks_like_pdf_asset(source_url, file_name)
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

    if is_direct_pdf:
        return {
            "downloadUrl": source_url,
            "sourceType": "public_pdf",
            "sourceLabel": f"Broker flyer: {file_name}",
            "sourceUrl": source_url,
        }

    # Common CRE file-share hosts (SharePoint / OneDrive / 1drv.ms / Box /
    # WeTransfer) and Google Drive *folder* links carry no direct file extension,
    # so none of the branches above matched. This is almost always real broker
    # payload the broker clearly intends as the message contents. We cannot
    # resolve the underlying file deterministically, but returning None here would
    # silently drop it and let the message be marked processed. FLAG it for manual
    # review instead so it stays visible (no downloadUrl -> callers surface it
    # rather than attempting a doomed download).
    if _is_file_share_host(source_url) or _is_drive_folder_url(source_url):
        return {
            "sourceType": "broker_file_share_link",
            "sourceLabel": f"Broker file-share link (needs manual review): {file_name}",
            "sourceUrl": source_url,
            "requiresManualReview": True,
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


def _match_tokens(*values: Any) -> List[str]:
    raw = " ".join(_clean(value).lower() for value in values if _clean(value))
    return [
        token
        for token in re.split(r"[^a-z0-9]+", raw)
        if len(token) >= 3 and not token.isdigit()
    ]


def _manifest_item_match_score(
    item: Dict[str, Any],
    *,
    address: str = "",
    city: str = "",
    source_url: str = "",
) -> int:
    haystack = " ".join(
        _clean(item.get(key)).lower()
        for key in (
            "name",
            "source_url",
            "drive_link",
            "property_image_source",
            "text",
        )
    )
    score = 0
    requested_url = _clean(source_url).lower()
    if requested_url and requested_url in haystack:
        score += 100
    for token in _match_tokens(address):
        if token in haystack:
            score += 15
    for token in _match_tokens(city):
        if token in haystack:
            score += 3
    return score


def select_property_image_candidate(
    pdf_manifest: List[Dict[str, Any]],
    *,
    address: str = "",
    city: str = "",
    source_url: str = "",
) -> Optional[Dict[str, Any]]:
    candidates: List[tuple[int, int, Dict[str, Any]]] = []
    for index, item in enumerate(pdf_manifest or []):
        item = item or {}
        candidate = _safe_candidate_from_manifest_item(item)
        if candidate:
            candidates.append((
                _manifest_item_match_score(
                    item,
                    address=address,
                    city=city,
                    source_url=source_url,
                ),
                index,
                candidate,
            ))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (-entry[0], entry[1]))
    return candidates[0][2]


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
