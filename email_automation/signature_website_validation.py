"""Validate the website link carried by an outgoing email signature.

WHY THIS EXISTS
---------------
Live testing proved that an outgoing signature whose website link points at an
UNREACHABLE or RESERVED placeholder domain reliably lands the message in the
spam folder, while byte-identical signature HTML pointing at a real reachable
domain arrives in the inbox. The HTML structure is exonerated; the *link target*
is the spam signal. Nothing in the product validated that field.

TWO TIERS, DELIBERATELY SPLIT
-----------------------------
Tier 1 -- OFFLINE (severity "block"). Purely syntactic: reserved/special-use
    domains, obvious placeholder hosts, private/loopback addresses, malformed
    URLs. These are cheap, deterministic, and cannot be wrong because of a
    transient failure. Tier 1 runs in the send path and NEUTRALISES the link
    (the anchor's href is removed; the visible text survives). The message still
    sends -- a bad signature link never stops a campaign.

Tier 2 -- NETWORK REACHABILITY (severity "warn"). A DNS/HTTP probe. This is
    NOT reliable enough to gate anything: an office Wi-Fi blip, an outbound
    firewall on the worker, or a site that is briefly down would otherwise
    silently kill a user's campaign. Tier 2 therefore:
      * never runs inside the send path,
      * never mutates outgoing HTML,
      * only ever produces severity "warn" for the user to look at.

FAIL-OPEN
---------
Every public entry point is wrapped so that ANY unexpected exception yields
"no findings" / "HTML unchanged". A bug in this validator must never be able to
stop mail from going out.

Dependency-free: standard library only (bs4 is used opportunistically for the
HTML rewrite because email_automation.utils already depends on it, and there is
a regex fallback if it is unavailable).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Feature flags -- BOTH DEFAULT OFF.
# --------------------------------------------------------------------------

#: Master flag. Off => this module is inert; get_email_footer returns byte-identical HTML.
SIGNATURE_WEBSITE_VALIDATION_ENV = "SITESIFT_SIGNATURE_WEBSITE_VALIDATION"

#: Sub-flag for the optional, non-blocking network probe. Requires the master flag.
SIGNATURE_WEBSITE_REACHABILITY_ENV = "SITESIFT_SIGNATURE_WEBSITE_REACHABILITY"

#: Seconds allowed for the whole Tier 2 probe of one URL.
SIGNATURE_WEBSITE_PROBE_TIMEOUT_ENV = "SITESIFT_SIGNATURE_WEBSITE_PROBE_TIMEOUT"


def _env_true(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() == "true"


def signature_website_validation_enabled() -> bool:
    """Tier 1 (offline) master switch. Default OFF."""
    return _env_true(SIGNATURE_WEBSITE_VALIDATION_ENV)


def signature_website_reachability_enabled() -> bool:
    """Tier 2 (network) switch. Default OFF, and inert unless Tier 1 is on."""
    return signature_website_validation_enabled() and _env_true(
        SIGNATURE_WEBSITE_REACHABILITY_ENV
    )


def _probe_timeout() -> float:
    try:
        return max(0.5, min(10.0, float(os.getenv(SIGNATURE_WEBSITE_PROBE_TIMEOUT_ENV) or 3.0)))
    except (TypeError, ValueError):
        return 3.0


# --------------------------------------------------------------------------
# Finding model
# --------------------------------------------------------------------------

SEVERITY_BLOCK = "block"   # Tier 1 only. Deterministic. Link is neutralised.
SEVERITY_WARN = "warn"     # Tier 2 only. Advisory. Nothing is changed or stopped.


@dataclass(frozen=True)
class SignatureWebsiteFinding:
    url: str
    host: str
    severity: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def _finding(url: str, host: str, severity: str, code: str, message: str) -> SignatureWebsiteFinding:
    return SignatureWebsiteFinding(
        url=url, host=host, severity=severity, code=code, message=message
    )


# --------------------------------------------------------------------------
# Tier 1 data: reserved / special-use / placeholder
# --------------------------------------------------------------------------

# RFC 2606, RFC 6761, RFC 6762, RFC 7686, RFC 8375, RFC 9476.
RESERVED_TLDS = frozenset({
    "test", "invalid", "localhost", "example", "local", "onion", "alt",
    "internal", "lan", "intranet", "private", "corp", "home",
})

# Suffixes reserved as whole names.
RESERVED_SUFFIXES = (
    "example.com", "example.net", "example.org", "example.edu",
    "home.arpa", "in-addr.arpa", "ip6.arpa",
)

# Placeholder LABELS. Matched against the label with "-" and "_" removed, so
# "your-company.com" matches but "beyourcompany.com" deliberately does not.
PLACEHOLDER_LABELS = frozenset({
    "yourcompany", "yourdomain", "yoursite", "yourwebsite", "yourbusiness",
    "yourbrand", "yourfirm", "youragency", "yourname", "yourcompanyname",
    "mycompany", "mydomain", "mysite", "mywebsite", "mybusiness", "mybrand",
    "ourcompany", "ourdomain", "oursite", "ourwebsite",
    "companyname", "companywebsite", "domainname", "sitename", "brandname",
    "brokeragename", "firmname",
    "changeme", "replaceme", "insertdomain", "inserturl", "insertwebsite",
    "todo", "tbd", "tba", "fixme", "placeholder", "dummy", "sample",
    "samplesite", "samplecompany", "lorem", "ipsum", "loremipsum",
    "notarealdomain", "nodomain", "nowebsite", "untitled",
    "website", "domain", "url",
})

# Generic "your<noun>" / "my<noun>" construction not enumerated above.
_GENERIC_PLACEHOLDER_RE = re.compile(
    r"^(?:your|my|our|the)(?:company|domain|site|website|business|brand|firm|agency|name|org)$"
)

# xxx / xxxx / aaaa style filler.
_FILLER_LABEL_RE = re.compile(r"^(?:x{3,}|a{4,}|z{4,}|asdf+|qwerty|test{1,2}|foo|bar|baz)$")

# Characters legal in a hostname label once IDNA-encoded.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Leftover mail-merge tokens.
# Alpha-led bracket tokens ONLY, matching email_automation.utils._SIGNATURE_PLACEHOLDER_TOKEN_RE.
# A bare "[^\]]*" would swallow the IPv6 literal in "http://[::1]/".
_UNRESOLVED_TOKEN_RE = re.compile(
    r"\[[A-Za-z][^\]\n]{0,60}\]|\{\{[^}\n]{0,60}\}\}|%[A-Za-z_]{2,40}%|\$\{[^}\n]{0,60}\}"
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


# --------------------------------------------------------------------------
# Tier 1: offline validation of ONE url
# --------------------------------------------------------------------------

def _normalize_candidate(raw: Any) -> str:
    """Mirror email_automation.utils._normalize_signature_url without importing it."""
    url = str(raw or "").strip()
    if not url:
        return ""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", url):
        return url
    return "https://" + url


def _idna_host(host: str) -> Optional[str]:
    """Lowercase + IDNA-encode a host. None means 'not encodable' => malformed."""
    host = host.strip().rstrip(".")
    if not host:
        return None
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            return None
    return host.lower()


def _ip_finding(url: str, host: str) -> Optional[SignatureWebsiteFinding]:
    """If host is an IP literal, judge it. Returns None if it is not an IP at all."""
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return None
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    ):
        return _finding(
            url, host, SEVERITY_BLOCK, "private_or_loopback_address",
            "The signature website points at a private or loopback address that no "
            "recipient can reach.",
        )
    return _finding(
        url, host, SEVERITY_BLOCK, "bare_ip_address",
        "The signature website is a bare IP address rather than a domain name.",
    )


def _reserved_finding(url: str, host: str, labels: List[str]) -> Optional[SignatureWebsiteFinding]:
    tld = labels[-1]
    if tld in RESERVED_TLDS:
        return _finding(
            url, host, SEVERITY_BLOCK, "reserved_tld",
            f"'.{tld}' is a reserved/special-use suffix that never resolves on the public internet.",
        )
    for suffix in RESERVED_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return _finding(
                url, host, SEVERITY_BLOCK, "reserved_domain",
                f"'{suffix}' is a documentation/reserved domain, not a real website.",
            )
    return None


# Second-level public suffixes, so the registrable label of "placeholder.co.uk"
# is "placeholder" and not "co". Not exhaustive -- a miss only costs one
# placeholder detection, never a false positive.
_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk", "ac.uk", "gov.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "com.br", "net.br", "com.mx", "com.ar",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "co.za", "org.za", "co.in", "net.in",
    "com.sg", "com.hk", "com.tw", "co.kr", "com.cn", "net.cn", "com.tr", "co.il",
    "com.pl", "com.ua", "co.th", "com.my", "com.ph", "com.vn", "co.id",
})


def _registrable_label(labels: List[str]) -> str:
    """The label immediately left of the public suffix."""
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return labels[-3]
    return labels[-2]


def _placeholder_finding(url: str, host: str, labels: List[str]) -> Optional[SignatureWebsiteFinding]:
    # ONLY the registrable label is judged. Judging every label would flag a real
    # host like "sub.domain.acmerealty.com" on its "domain" subdomain, and the
    # public suffix itself would collide with real gTLDs such as ".website".
    label = _registrable_label(labels)
    squashed = label.replace("-", "").replace("_", "")
    if not squashed:
        return None
    if (
        squashed in PLACEHOLDER_LABELS
        or _GENERIC_PLACEHOLDER_RE.match(squashed)
        or _FILLER_LABEL_RE.match(squashed)
    ):
        return _finding(
            url, host, SEVERITY_BLOCK, "placeholder_host",
            f"'{label}' looks like an unfilled placeholder rather than a real domain.",
        )
    return None


def validate_signature_website_url(raw: Any) -> Optional[SignatureWebsiteFinding]:
    """TIER 1. Offline, deterministic, no network. None means 'nothing wrong'.

    Fail-open: any unexpected exception returns None.
    """
    try:
        return _validate_offline(raw)
    except Exception:  # pragma: no cover - fail-open guard
        logger.exception("signature website validation failed open")
        return None


def _validate_offline(raw: Any) -> Optional[SignatureWebsiteFinding]:
    original = str(raw or "").strip()
    if not original:
        return None  # No link is rendered at all; nothing to judge.

    if _UNRESOLVED_TOKEN_RE.search(original):
        return _finding(
            original, "", SEVERITY_BLOCK, "unresolved_placeholder_token",
            "The signature website still contains an unsubstituted template token.",
        )

    url = _normalize_candidate(original)
    try:
        parts = urlsplit(url)
    except ValueError:
        return _finding(original, "", SEVERITY_BLOCK, "malformed_url",
                        "The signature website is not a parseable URL.")

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return _finding(
            original, "", SEVERITY_BLOCK, "unsupported_scheme",
            f"'{scheme or 'missing'}:' is not an http(s) website link.",
        )

    try:
        raw_host = parts.hostname or ""
    except ValueError:
        return _finding(original, "", SEVERITY_BLOCK, "malformed_url",
                        "The signature website has an unparseable host.")

    if not raw_host:
        return _finding(original, "", SEVERITY_BLOCK, "malformed_url",
                        "The signature website has no host.")

    if any(ch.isspace() for ch in raw_host):
        return _finding(original, raw_host, SEVERITY_BLOCK, "malformed_url",
                        "The signature website host contains whitespace.")

    host = _idna_host(raw_host)
    if not host:
        return _finding(original, raw_host, SEVERITY_BLOCK, "malformed_url",
                        "The signature website host cannot be encoded as a hostname.")

    if len(host) > 253:
        return _finding(original, host, SEVERITY_BLOCK, "malformed_url",
                        "The signature website host is longer than a legal hostname.")

    ip_verdict = _ip_finding(url, host)
    if ip_verdict is not None:
        return ip_verdict

    if host == "localhost" or host.endswith(".localhost"):
        return _finding(url, host, SEVERITY_BLOCK, "private_or_loopback_address",
                        "The signature website points at localhost.")

    labels = host.split(".")
    if len(labels) < 2:
        return _finding(
            url, host, SEVERITY_BLOCK, "no_public_suffix",
            "The signature website has no domain suffix, so it only resolves on a private network.",
        )

    for label in labels:
        if not _LABEL_RE.match(label):
            return _finding(original, host, SEVERITY_BLOCK, "malformed_url",
                            f"'{label}' is not a legal hostname label.")

    tld = labels[-1]
    if len(tld) < 2 or tld.isdigit():
        return _finding(original, host, SEVERITY_BLOCK, "malformed_url",
                        f"'.{tld}' is not a legal top-level domain.")

    reserved = _reserved_finding(url, host, labels)
    if reserved is not None:
        return reserved

    placeholder = _placeholder_finding(url, host, labels)
    if placeholder is not None:
        return placeholder

    return None


# --------------------------------------------------------------------------
# Tier 2: OPTIONAL, NON-BLOCKING reachability probe
# --------------------------------------------------------------------------

def check_signature_website_reachable(
    raw: Any, *, timeout: Optional[float] = None
) -> Optional[SignatureWebsiteFinding]:
    """TIER 2. Network probe. ONLY ever returns severity 'warn' -- never 'block'.

    A transient DNS or TCP failure here means the user sees an advisory, nothing
    more. This function must never be consulted by code that decides whether a
    message is sent or how its HTML is rendered.

    Fail-open: any unexpected exception returns None.
    """
    try:
        return _probe(raw, timeout if timeout is not None else _probe_timeout())
    except Exception:  # pragma: no cover - fail-open guard
        logger.exception("signature website reachability probe failed open")
        return None


def _probe(raw: Any, timeout: float) -> Optional[SignatureWebsiteFinding]:
    url = _normalize_candidate(raw)
    if not url:
        return None
    parts = urlsplit(url)
    host = _idna_host(parts.hostname or "") or ""
    if not host:
        return None

    try:
        socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                           proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return _finding(
            url, host, SEVERITY_WARN, "dns_unresolvable",
            "This website did not resolve in DNS when checked. If that is not a "
            "temporary network problem, mail carrying this link is likely to be "
            "filtered as spam.",
        )
    except OSError:
        return None  # Local network problem -- say nothing rather than cry wolf.

    # A server that ANSWERS at all (including 4xx/5xx) counts as reachable; only a
    # connection-level failure is worth mentioning.
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "SiteSift-LinkCheck/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return None
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError):
        return _finding(
            url, host, SEVERITY_WARN, "http_unreachable",
            "This website resolved in DNS but did not answer an HTTP request when "
            "checked. If that is not a temporary outage, mail carrying this link is "
            "likely to be filtered as spam.",
        )


# --------------------------------------------------------------------------
# URL extraction from a signature (structured field OR rendered HTML)
# --------------------------------------------------------------------------

_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""", re.IGNORECASE | re.DOTALL)
# The trailing "(?![\w.\-]*@)" stops "jane.doe" in "jane.doe@acmerealty.com"
# from being read as a bare domain.
_BARE_URL_RE = re.compile(
    r"(?<![\w@.])((?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?:/[^\s<>\"']*)?)"
    r"(?![\w.\-]*@)",
    re.IGNORECASE,
)
_SKIP_SCHEMES = ("mailto:", "tel:", "cid:", "data:", "sms:", "callto:", "#")

# Link targets that are structurally part of the signature chrome, not the
# user's own website (social icons etc.). Judged, but never neutralised.
_CHROME_HOSTS = frozenset({
    "linkedin.com", "www.linkedin.com", "twitter.com", "www.twitter.com",
    "x.com", "www.x.com", "facebook.com", "www.facebook.com",
    "instagram.com", "www.instagram.com",
})


def extract_signature_link_targets(signature: Any) -> List[str]:
    """Every href in a signature's HTML, in document order, minus mailto/tel/cid."""
    text = str(signature or "")
    if not text.strip():
        return []
    out: List[str] = []
    for match in _HREF_RE.finditer(text):
        href = (match.group("url") or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue
        out.append(href)
    return out


def extract_plain_text_website_candidates(signature: Any) -> List[str]:
    """Bare domains in a PLAIN-TEXT signature (no anchors present).

    Reported only. Plain text is never rewritten -- silently editing a user's
    own words is worse than the spam risk.
    """
    text = str(signature or "")
    if not text.strip() or "<a" in text.lower():
        return []
    out: List[str] = []
    for match in _BARE_URL_RE.finditer(text):
        candidate = match.group(1).strip().rstrip(".,;:)")
        if "@" in candidate:
            continue
        out.append(candidate)
    return out


def inspect_signature_websites(
    *,
    website: Any = None,
    signature_html: Any = None,
    check_reachability: bool = False,
) -> List[SignatureWebsiteFinding]:
    """All findings for a signature. Tier 2 runs ONLY when check_reachability=True.

    Fail-open: any unexpected exception returns [].
    """
    try:
        return _inspect(website, signature_html, check_reachability)
    except Exception:  # pragma: no cover - fail-open guard
        logger.exception("signature website inspection failed open")
        return []


def _inspect(website: Any, signature_html: Any, check_reachability: bool) -> List[SignatureWebsiteFinding]:
    candidates: List[str] = []
    if str(website or "").strip():
        candidates.append(str(website).strip())
    candidates.extend(extract_signature_link_targets(signature_html))
    candidates.extend(extract_plain_text_website_candidates(signature_html))

    findings: List[SignatureWebsiteFinding] = []
    seen_offline: set = set()
    reachable_queue: List[str] = []

    for candidate in candidates:
        # Dedupe on the NORMALISED url so the structured "website" field and the
        # rendered href that was built from it are recognised as one target.
        key = _normalize_candidate(candidate).lower().rstrip("/")
        if key in seen_offline:
            continue
        seen_offline.add(key)
        verdict = validate_signature_website_url(candidate)
        if verdict is not None:
            findings.append(verdict)
            continue
        reachable_queue.append(candidate)

    if check_reachability:
        for candidate in reachable_queue:
            host = _idna_host(urlsplit(_normalize_candidate(candidate)).hostname or "") or ""
            if host in _CHROME_HOSTS:
                continue
            warn = check_signature_website_reachable(candidate)
            if warn is not None:
                findings.append(warn)

    return findings


# --------------------------------------------------------------------------
# Tier 1 enforcement: neutralise a dangerous link inside signature HTML
# --------------------------------------------------------------------------

def _blocked_hrefs(html: str) -> Dict[str, SignatureWebsiteFinding]:
    blocked: Dict[str, SignatureWebsiteFinding] = {}
    for href in extract_signature_link_targets(html):
        verdict = validate_signature_website_url(href)
        if verdict is not None and verdict.severity == SEVERITY_BLOCK:
            blocked[href.strip()] = verdict
    return blocked


def _neutralize_with_bs4(html: str, blocked: Dict[str, SignatureWebsiteFinding]) -> Optional[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is already a hard dependency
        return None
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if href in blocked:
            del anchor.attrs["href"]
            anchor.attrs.pop("target", None)
            anchor.attrs.pop("rel", None)
            changed = True
    return str(soup) if changed else html


def _neutralize_with_regex(html: str, blocked: Dict[str, SignatureWebsiteFinding]) -> str:
    def _strip(match: "re.Match") -> str:
        tag = match.group(0)
        href = (match.group("url") or "").strip()
        if href not in blocked:
            return tag
        tag = re.sub(r"""\s+href\s*=\s*(["']).*?\1""", "", tag, flags=re.IGNORECASE | re.DOTALL)
        tag = re.sub(r"""\s+target\s*=\s*(["']).*?\1""", "", tag, flags=re.IGNORECASE | re.DOTALL)
        tag = re.sub(r"""\s+rel\s*=\s*(["']).*?\1""", "", tag, flags=re.IGNORECASE | re.DOTALL)
        return tag

    return _HREF_RE.sub(_strip, html)


def apply_signature_website_policy(footer_html: Any) -> Tuple[str, List[SignatureWebsiteFinding]]:
    """TIER 1 ENFORCEMENT, called from the send path.

    Returns ``(html, findings)``. When the master flag is off, or nothing is
    wrong, or anything at all goes sideways, ``html`` is returned UNCHANGED and
    ``findings`` is empty. Only anchors whose href fails an OFFLINE check are
    de-linked; the visible link text is preserved so the signature still reads
    correctly. No network call is ever made from here.
    """
    html = footer_html if isinstance(footer_html, str) else ""
    try:
        if not html.strip() or not signature_website_validation_enabled():
            return html, []
        blocked = _blocked_hrefs(html)
        if not blocked:
            return html, []
        rewritten = _neutralize_with_bs4(html, blocked)
        if rewritten is None:
            rewritten = _neutralize_with_regex(html, blocked)
        for finding in blocked.values():
            logger.warning(
                "signature website neutralised: code=%s host=%s", finding.code, finding.host
            )
        return rewritten, list(blocked.values())
    except Exception:  # pragma: no cover - fail-open guard
        logger.exception("signature website policy failed open; sending signature unchanged")
        return html, []


# --------------------------------------------------------------------------
# Advisory pass -- for campaign-entry code that has the user profile in hand
# --------------------------------------------------------------------------

def signature_website_advisory(user_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Both tiers, no mutation, safe to call once per run. NEVER blocks anything.

    Returns a JSON-serialisable advisory suitable for writing onto the user
    profile for the frontend to render. Empty dict means 'nothing to say'.
    """
    try:
        if not signature_website_validation_enabled():
            return {}
        user_data = user_data or {}
        professional = user_data.get("professionalSignature")
        if not isinstance(professional, dict):
            professional = {}
        findings = inspect_signature_websites(
            website=professional.get("website"),
            signature_html=user_data.get("emailSignature"),
            check_reachability=signature_website_reachability_enabled(),
        )
        if not findings:
            return {}
        return {
            "hasBlockingFinding": any(f.severity == SEVERITY_BLOCK for f in findings),
            "findings": [f.to_dict() for f in findings],
        }
    except Exception:  # pragma: no cover - fail-open guard
        logger.exception("signature website advisory failed open")
        return {}
