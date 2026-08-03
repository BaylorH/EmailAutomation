"""Sent Items reconciliation helpers for ambiguous Graph send failures.

Graph send endpoints can time out after Microsoft has accepted a message. Before
retrying a stored send, search the sender's Sent Items for a matching message so
we can stop duplicate sends and surface a reconciliation item instead.
"""

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .utils import exponential_backoff_request, strip_html_tags


class SentMailGuardLookupError(Exception):
    """Raised when Sent Items cannot be checked safely before a retry."""


GRAPH_IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'
GRAPH_EXACT_SENT_ATTACHMENT_LIMIT = 32
GRAPH_EXACT_SENT_ATTACHMENT_PAGE_LIMIT = 4


def graph_headers_with_immutable_id(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copied header map requesting stable Graph message IDs."""
    copied = dict(headers or {})
    prefer_key = next(
        (key for key in copied if str(key).strip().lower() == "prefer"),
        "Prefer",
    )
    existing = str(copied.get(prefer_key) or "").strip()
    tokens = [token.strip() for token in existing.split(",") if token.strip()]
    if not any(
        token.lower() == GRAPH_IMMUTABLE_ID_PREFER.lower()
        for token in tokens
    ):
        tokens.append(GRAPH_IMMUTABLE_ID_PREFER)
    copied[prefer_key] = ", ".join(tokens)
    return copied


def find_exact_sent_message_by_immutable_id(
    headers: Dict[str, str],
    immutable_message_id: str,
    *,
    recipient: Optional[str] = None,
    to_recipients: Optional[Any] = None,
    cc_recipients: Optional[Any] = None,
    require_no_bcc: bool = False,
    require_attachment_proof: bool = False,
    body: Optional[str] = None,
    canonical_body_hash: Optional[str] = None,
    subject: Optional[str] = None,
    conversation_id: Optional[str] = None,
    base: str = "https://graph.microsoft.com/v1.0",
    attempts: int = 4,
) -> Optional[Dict[str, Any]]:
    """Read the exact draft/sent object by stable ID and prove it is Sent.

    Graph can briefly return 404 while moving a just-sent draft into Sent
    Items.  Exhausting that bounded read is therefore *unknown*, never proof
    that the provider did not send.  Heuristic mailbox searches are not used.
    """
    message_id = str(immutable_message_id or "").strip()
    if not message_id:
        raise SentMailGuardLookupError(
            "exact Graph Sent confirmation requires an immutable message id"
        )
    try:
        attempts = int(attempts)
    except (TypeError, ValueError) as exc:
        raise SentMailGuardLookupError(
            "exact Graph Sent confirmation attempts are malformed"
        ) from exc
    if attempts < 1 or attempts > 8:
        raise SentMailGuardLookupError(
            "exact Graph Sent confirmation attempts must be between 1 and 8"
        )
    if subject is not None and (
        not isinstance(subject, str) or not subject.strip()
    ):
        raise SentMailGuardLookupError(
            "exact Graph Sent confirmation subject is malformed"
        )

    immutable_headers = graph_headers_with_immutable_id(headers)
    encoded_id = quote(message_id, safe="")
    params = {
        "$select": (
            "id,isDraft,internetMessageId,conversationId,subject,toRecipients,"
            "ccRecipients,bccRecipients,sentDateTime,body,bodyPreview"
        )
    }
    last_error: Optional[Exception] = None
    saw_not_ready = False
    for attempt in range(attempts):
        try:
            response = requests.get(
                f"{base}/me/messages/{encoded_id}",
                headers=immutable_headers,
                params=params,
                timeout=30,
            )
            if response.status_code == 404:
                saw_not_ready = True
            elif response.status_code != 200:
                last_error = RuntimeError(
                    "exact Graph Sent confirmation returned HTTP "
                    f"{response.status_code}"
                )
            else:
                message = response.json() or {}
                if str(message.get("id") or "").strip() != message_id:
                    raise SentMailGuardLookupError(
                        "exact Graph Sent confirmation returned a different id"
                    )
                if message.get("isDraft") is not False:
                    saw_not_ready = True
                elif coerce_utc_datetime(message.get("sentDateTime")) is None:
                    saw_not_ready = True
                else:
                    expected_conversation = str(conversation_id or "").strip()
                    actual_conversation = str(
                        message.get("conversationId") or ""
                    ).strip()
                    if (
                        expected_conversation
                        and actual_conversation != expected_conversation
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation conversation drifted"
                        )
                    if (
                        subject is not None
                        and message.get("subject") != subject
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation subject drifted"
                        )
                    expected_recipient = _normalize_email(recipient)
                    if (
                        expected_recipient
                        and expected_recipient not in _message_recipients(message)
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation recipient drifted"
                        )
                    exact_envelope_requested = bool(
                        to_recipients is not None
                        or cc_recipients is not None
                        or require_no_bcc
                    )
                    if to_recipients is not None and (
                        _recipient_projection(
                            message.get("toRecipients"),
                            field="actual To",
                        )
                        != _recipient_projection(
                            to_recipients,
                            field="expected To",
                        )
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation To recipient envelope drifted"
                        )
                    if cc_recipients is not None and (
                        _recipient_projection(
                            message.get("ccRecipients"),
                            field="actual Cc",
                        )
                        != _recipient_projection(
                            cc_recipients,
                            field="expected Cc",
                        )
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation Cc recipient envelope drifted"
                        )
                    if require_no_bcc and (message.get("bccRecipients") or []):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation Bcc envelope drifted"
                        )
                    if canonical_body_hash and (
                        canonical_graph_body_hash(message.get("body"))
                        != canonical_body_hash
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation canonical body drifted"
                        )
                    if (
                        body
                        and not canonical_body_hash
                        and exact_envelope_requested
                        and canonical_graph_body_hash(message.get("body"))
                        != canonical_graph_body_hash(body)
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation canonical body drifted"
                        )
                    if (
                        body
                        and not canonical_body_hash
                        and not exact_envelope_requested
                        and not _body_matches(body, message)
                    ):
                        raise SentMailGuardLookupError(
                            "exact Graph Sent confirmation body drifted"
                        )
                    if require_attachment_proof:
                        message = dict(message)
                        message["attachments"] = _read_exact_message_attachments(
                            base,
                            encoded_id,
                            immutable_headers,
                        )
                    return dict(message)
        except SentMailGuardLookupError:
            raise
        except Exception as exc:
            last_error = exc

        if attempt < attempts - 1:
            import time

            time.sleep(0.5 * (attempt + 1))

    if last_error and not saw_not_ready:
        raise SentMailGuardLookupError(str(last_error))
    return None


def _read_exact_message_attachments(
    base: str,
    encoded_message_id: str,
    headers: Dict[str, str],
) -> list[Dict[str, Any]]:
    """Read a bounded attachment collection for the exact immutable message."""
    next_url = f"{base}/me/messages/{encoded_message_id}/attachments"
    params = {
        "$select": (
            "id,name,contentType,contentBytes,contentId,isInline"
        )
    }
    attachments = []
    for page_index in range(GRAPH_EXACT_SENT_ATTACHMENT_PAGE_LIMIT):
        response = requests.get(
            next_url,
            headers=headers,
            params=params if page_index == 0 else None,
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                "exact Graph Sent attachment confirmation returned HTTP "
                f"{response.status_code}"
            )
        payload = response.json() or {}
        page = payload.get("value")
        if not isinstance(page, list) or any(
            not isinstance(item, dict) for item in page
        ):
            raise SentMailGuardLookupError(
                "exact Graph Sent attachment collection is malformed"
            )
        attachments.extend(dict(item) for item in page)
        if len(attachments) > GRAPH_EXACT_SENT_ATTACHMENT_LIMIT:
            raise SentMailGuardLookupError(
                "exact Graph Sent attachment collection exceeds the safe bound"
            )
        next_link = payload.get("@odata.nextLink")
        if not next_link:
            return attachments
        if not isinstance(next_link, str) or not _same_graph_origin(base, next_link):
            raise SentMailGuardLookupError(
                "exact Graph Sent attachment pagination left the Graph origin"
            )
        next_url = next_link
    raise SentMailGuardLookupError(
        "exact Graph Sent attachment pagination exceeds the safe bound"
    )


def _same_graph_origin(base: str, next_link: str) -> bool:
    """True when ``next_link`` shares the scheme+host of ``base``.

    Graph pagination links are request-controlled URLs; we reuse the caller's
    Authorization bearer against them, so the origin must match the base Graph
    endpoint before we follow (SSRF / token-replay defense-in-depth).
    """
    try:
        base_parts = urlsplit(base)
        link_parts = urlsplit(next_link)
    except ValueError:
        return False
    return (
        link_parts.scheme == base_parts.scheme
        and link_parts.netloc.lower() == base_parts.netloc.lower()
    )


def coerce_utc_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_datetime"):
            value = value.to_datetime()
        elif hasattr(value, "timestamp") and not isinstance(value, datetime):
            value = datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)
        elif isinstance(value, (int, float)):
            value = datetime.fromtimestamp(float(value), tz=timezone.utc)
        elif isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    except Exception:
        return None
    return None


def sent_after_from_retry_data(data: Dict[str, Any], *, fallback_hours: int = 48) -> datetime:
    for key in (
        "lastSendAttemptAt",
        "lastFailedAt",
        "lastRetryAt",
        "updatedAt",
        "processingAt",
        "createdAt",
    ):
        parsed = coerce_utc_datetime((data or {}).get(key))
        if parsed:
            return parsed - timedelta(seconds=30)
    return datetime.now(timezone.utc) - timedelta(hours=fallback_hours)


def _escape_odata_string(value: Any) -> str:
    """Escape a value for embedding inside an OData single-quoted string."""
    return str(value or "").replace("'", "''")


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_text(value: Any) -> str:
    text = strip_html_tags(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _normalize_graph_body_text(value: Any) -> str:
    """Normalize provider formatting without erasing exact text case."""
    # HTMLParser has already decoded character references and removed markup
    # before visible text and image alt text reach this helper.  Treat that
    # input as plain text so decoded angle-bracket content is not parsed away.
    text = str(value or "")
    return re.sub(r"\s+", " ", text).strip()


_BODY_RESOURCE_ATTRIBUTES = {
    "action",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "poster",
    "src",
    "srcset",
}
_BODY_NONVISIBLE_TAGS = {"script", "style", "template", "noscript"}
_CSS_URL_RE = re.compile(
    r"url\(\s*(['\"]?)(.*?)\1\s*\)",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?!url\()(['\"])(.*?)\1",
    re.IGNORECASE,
)


def _canonical_body_resource(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        userinfo, separator, host_port = parsed.netloc.rpartition("@")
        userinfo_prefix = f"{userinfo}@" if separator else ""
        if host_port.startswith("["):
            bracket_end = host_port.find("]")
            if bracket_end >= 0:
                normalized_host_port = (
                    f"[{host_port[1:bracket_end].lower()}]"
                    f"{host_port[bracket_end + 1:]}"
                )
            else:
                normalized_host_port = host_port
        else:
            hostname, port_separator, port = host_port.partition(":")
            normalized_host_port = (
                f"{hostname.lower()}{port_separator}{port}"
            )
        return urlunsplit(
            (
                scheme,
                f"{userinfo_prefix}{normalized_host_port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    if scheme == "cid":
        return f"cid:{parsed.path.strip('<>')}"
    if scheme == "mailto":
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        return f"mailto:{parsed.path.lower()}{query}{fragment}"
    if scheme == "data":
        return "data:sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw


class _SemanticBodyParser(HTMLParser):
    """Extract rendered text and behavior-bearing resources from HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.resources = []
        self._nonvisible_depth = 0
        self._style_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag in _BODY_NONVISIBLE_TAGS:
            self._nonvisible_depth += 1
        if tag == "style":
            self._style_depth += 1
        normalized_attrs = []
        for raw_name, raw_value in attrs:
            name = str(raw_name or "").lower()
            value = str(raw_value or "").strip()
            if name in _BODY_RESOURCE_ATTRIBUTES:
                normalized_attrs.append(
                    [tag, name, _canonical_body_resource(value)]
                )
            elif name == "style":
                for _quote, style_url in _CSS_URL_RE.findall(value):
                    normalized_attrs.append(
                        [tag, "style:resource", _canonical_body_resource(style_url)]
                    )
        if tag == "img":
            attributes = {
                str(name or "").lower(): str(value or "")
                for name, value in attrs
            }
            normalized_attrs.append(
                [tag, "alt", _normalize_graph_body_text(attributes.get("alt"))]
            )
        # Attribute order is formatting, not message behavior. DOM resource
        # order remains significant, while resources on one element are
        # compared as a canonical set.
        self.resources.extend(sorted(normalized_attrs))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if str(tag or "").lower() in _BODY_NONVISIBLE_TAGS:
            self._nonvisible_depth -= 1
        if str(tag or "").lower() == "style":
            self._style_depth -= 1

    def handle_endtag(self, tag):
        tag = str(tag or "").lower()
        if (
            tag in _BODY_NONVISIBLE_TAGS
            and self._nonvisible_depth > 0
        ):
            self._nonvisible_depth -= 1
        if tag == "style" and self._style_depth > 0:
            self._style_depth -= 1

    def handle_data(self, data):
        if self._style_depth > 0:
            style_resources = [
                ["style", "style:resource", _canonical_body_resource(resource)]
                for _quote, resource in _CSS_URL_RE.findall(str(data or ""))
            ]
            style_resources.extend(
                ["style", "style:resource", _canonical_body_resource(resource)]
                for _quote, resource in _CSS_IMPORT_RE.findall(str(data or ""))
            )
            self.resources.extend(sorted(style_resources))
        elif self._nonvisible_depth == 0 and str(data or "").strip():
            self.text_parts.append(str(data))


def canonical_graph_body_hash(value: Any) -> str:
    """Hash Graph-tolerant body semantics, including links and resources.

    Wrapper tags, style-only formatting, and attribute order are intentionally
    ignored.  Rendered text and ordered behavior-bearing URI attributes remain
    part of the proof, so a changed href, image source/CID, form action, or CSS
    resource cannot collide with the frozen prepared envelope.
    """
    if isinstance(value, dict):
        value = value.get("content")
    parser = _SemanticBodyParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        # Malformed provider HTML must not collapse to a text-only proof.
        projection = {
            "parseError": True,
            "rawHash": hashlib.sha256(
                str(value or "").encode("utf-8")
            ).hexdigest(),
        }
    else:
        projection = {
            "visibleText": _normalize_graph_body_text(" ".join(parser.text_parts)),
            "resources": parser.resources,
        }
    return hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _recipient_projection(values: Any, *, field: str) -> list[str]:
    """Return an exact normalized recipient projection or fail closed."""
    if values is None:
        raw_values = []
    elif isinstance(values, (str, bytes)):
        raw_values = [values]
    else:
        try:
            raw_values = list(values)
        except TypeError as exc:
            raise SentMailGuardLookupError(
                f"exact Graph Sent confirmation {field} recipients are malformed"
            ) from exc

    normalized = []
    for value in raw_values:
        if isinstance(value, dict):
            email_address = value.get("emailAddress")
            address = (
                email_address.get("address")
                if isinstance(email_address, dict)
                else None
            )
        else:
            address = value
        address = _normalize_email(address)
        if not address:
            raise SentMailGuardLookupError(
                f"exact Graph Sent confirmation {field} recipient is malformed"
            )
        normalized.append(address)
    if len(set(normalized)) != len(normalized):
        raise SentMailGuardLookupError(
            f"exact Graph Sent confirmation {field} recipients contain duplicates"
        )
    return sorted(normalized)


# Reply/forward prefixes across common regional Outlook locales. English
# (RE/FW/FWD) plus German (AW/WG), Swedish (SV/VB), French (TR), Dutch (VS/DW),
# etc. Stripped before subject comparison so a localized prefix on the Sent
# Items copy cannot hide an already-sent / continuation message.
_REPLY_PREFIX_RE = re.compile(r"^((aw|sv|tr|vs|re|fw|fwd|wg):\s*)+")


def _strip_reply_prefixes(subject: str) -> str:
    return _REPLY_PREFIX_RE.sub("", subject)


def _normalize_subject(value: Any) -> str:
    subject = _normalize_text(value)
    return _strip_reply_prefixes(subject)


def _message_recipients(message: Dict[str, Any]) -> set:
    recipients = set()
    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in message.get(key) or []:
            address = ((item or {}).get("emailAddress") or {}).get("address")
            normalized = _normalize_email(address)
            if normalized:
                recipients.add(normalized)
    return recipients


def _message_body_text(message: Dict[str, Any]) -> str:
    body = message.get("body") or {}
    return body.get("content") or message.get("bodyPreview") or ""


def _body_matches(expected_body: str, message: Dict[str, Any]) -> bool:
    expected = _normalize_text(expected_body)
    actual = _normalize_text(_message_body_text(message))
    preview = _normalize_text(message.get("bodyPreview"))
    if not expected:
        return False
    for candidate in (actual, preview):
        if not candidate:
            continue
        if candidate == expected:
            return True
        if len(expected) >= 24 and expected[:800] in candidate:
            return True
        if len(expected) < 24 and candidate.startswith(f"{expected} "):
            return True
    return False


def _subject_matches(expected_subject: Optional[str], message: Dict[str, Any]) -> bool:
    if not expected_subject:
        return True
    expected = _normalize_subject(expected_subject)
    actual = _normalize_subject(message.get("subject"))
    if not expected or not actual:
        return False
    return expected == actual


def _has_enough_retry_identity(
    *,
    subject: Optional[str],
    conversation_id: Optional[str],
    body: str,
) -> bool:
    if conversation_id or _normalize_subject(subject):
        return True
    return len(_normalize_text(body)) >= 80


def _message_identity(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": message.get("id"),
        "sentMessageId": message.get("id"),
        "internetMessageId": message.get("internetMessageId"),
        "conversationId": message.get("conversationId"),
        "subject": message.get("subject"),
        "sentDateTime": message.get("sentDateTime"),
    }


def send_result_from_sent_match(match: Dict[str, Any], recipient: str) -> Dict[str, Any]:
    if not match or not recipient:
        return {}
    result = {
        "sent": [recipient],
        "sentMessageIds": {},
        "internetMessageIds": {},
        "conversationIds": {},
    }
    sent_message_id = match.get("sentMessageId") or match.get("id")
    if sent_message_id:
        result["sentMessageIds"][recipient] = sent_message_id
    if match.get("internetMessageId"):
        result["internetMessageIds"][recipient] = match.get("internetMessageId")
    if match.get("conversationId"):
        result["conversationIds"][recipient] = match.get("conversationId")
    return result


def find_matching_sent_message_for_retry(
    headers: Dict[str, str],
    *,
    recipient: str,
    body: str,
    subject: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sent_after: Optional[datetime] = None,
    base: str = "https://graph.microsoft.com/v1.0",
    attempts: int = 2,
) -> Optional[Dict[str, Any]]:
    """Return a matching Sent Items message if a prior failed retry likely sent."""
    recipient = _normalize_email(recipient)
    if not recipient or not body:
        return None
    if not _has_enough_retry_identity(subject=subject, conversation_id=conversation_id, body=body):
        raise SentMailGuardLookupError(
            "not enough unique message identity to verify Sent Items before retry"
        )

    sent_after_utc = (sent_after or (datetime.now(timezone.utc) - timedelta(hours=48))).astimezone(timezone.utc)
    params = {
        "$orderby": "sentDateTime desc",
        "$top": "25",
        "$select": "id,internetMessageId,conversationId,subject,toRecipients,ccRecipients,bccRecipients,sentDateTime,body,bodyPreview",
        "$filter": f"sentDateTime ge {sent_after_utc.isoformat().replace('+00:00', 'Z')}",
    }

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/mailFolders/SentItems/messages",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
            )
            if response.status_code != 200:
                last_error = RuntimeError(f"Sent Items lookup returned HTTP {response.status_code}")
                continue
            for message in (response.json() or {}).get("value", []):
                conv_matches = (
                    bool(conversation_id)
                    and message.get("conversationId") == conversation_id
                )
                if conversation_id and not conv_matches:
                    continue
                if recipient not in _message_recipients(message):
                    continue
                if not _body_matches(body, message):
                    continue
                # Strong identity — matching conversationId + recipient + body —
                # wins over a localized/regional subject prefix our normalizer
                # might not strip. Only let the subject veto a candidate when we
                # do NOT already have a conversationId match to anchor identity.
                if not conv_matches and not _subject_matches(subject, message):
                    continue
                return _message_identity(message)
        except Exception as exc:
            last_error = exc
            print(f"   ⚠️ Sent Items retry guard lookup failed: {exc}")

        if attempt < attempts - 1:
            import time

            time.sleep(0.5 * (attempt + 1))

    if last_error:
        raise SentMailGuardLookupError(str(last_error))
    return None


def find_sent_conversation_continuation_for_retry(
    headers: Dict[str, str],
    *,
    conversation_id: Optional[str],
    sent_after: Optional[datetime],
    base: str = "https://graph.microsoft.com/v1.0",
    attempts: int = 2,
) -> Optional[Dict[str, Any]]:
    """Return newer Sent Items metadata when the conversation moved on.

    This guard is deliberately lighter than find_matching_sent_message_for_retry:
    it does not prove our exact draft already sent, so it must not be used as a
    successful-send reconciliation. It only answers whether a human/user sent
    anything in the same conversation after a failed/queued retry point, which
    means automated stale retry work should stop for manual review.

    Privacy rule: select metadata only. Do not fetch body or bodyPreview.
    """
    if not conversation_id:
        return None
    sent_after_utc = coerce_utc_datetime(sent_after)
    if not sent_after_utc:
        # Fail CLOSED: an unusable sent_after means we cannot bound the lookup,
        # so we must not silently return "no continuation" (which would let a
        # stale draft go out). Raise so the caller moves the item to manual
        # review instead of retrying blind.
        raise SentMailGuardLookupError(
            "unusable sent_after for Sent Items continuation guard; failing closed"
        )

    top = 10
    # Scope the query to the target conversation SERVER-SIDE and page through
    # @odata.nextLink. Filtering only by sentDateTime and capping at the newest
    # $top unscoped sends lets a user continuation buried past those sends hide,
    # so the scheduler would send the stale draft.
    filter_expr = (
        f"sentDateTime ge {sent_after_utc.isoformat().replace('+00:00', 'Z')}"
        f" and conversationId eq '{_escape_odata_string(conversation_id)}'"
    )
    params = {
        "$orderby": "sentDateTime desc",
        "$top": str(top),
        "$select": "id,internetMessageId,conversationId,subject,toRecipients,sentDateTime",
        "$filter": filter_expr,
    }

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            url = f"{base}/me/mailFolders/SentItems/messages"
            request_params: Optional[Dict[str, str]] = params
            truncated = False
            page_error = False

            while True:
                response = exponential_backoff_request(
                    lambda u=url, p=request_params: requests.get(
                        u,
                        headers=headers,
                        params=p,
                        timeout=30,
                    )
                )
                if response.status_code != 200:
                    last_error = RuntimeError(
                        f"Sent Items lookup returned HTTP {response.status_code}"
                    )
                    page_error = True
                    break

                payload = response.json() or {}
                messages = payload.get("value", []) or []
                for message in messages:
                    if message.get("conversationId") != conversation_id:
                        continue
                    sent_time = coerce_utc_datetime(message.get("sentDateTime"))
                    if sent_time and sent_time < sent_after_utc:
                        continue
                    identity = _message_identity(message)
                    identity["recipientCount"] = len(message.get("toRecipients") or [])
                    return identity

                # A full page with no in-conversation match means results may be
                # truncated (more could live past this page); remember that so we
                # can fail closed rather than declare "no continuation".
                if len(messages) >= top:
                    truncated = True

                next_link = payload.get("@odata.nextLink")
                if next_link and _same_graph_origin(base, next_link):
                    url = next_link
                    request_params = None
                    continue
                if next_link:
                    # Defense-in-depth: never replay the Authorization bearer to
                    # an origin Graph did not vouch for (compromised proxy / future
                    # refactor that widens `base`). Fail CLOSED on an unexpected
                    # nextLink host rather than following it with our token.
                    last_error = RuntimeError(
                        "Unexpected @odata.nextLink host from Graph API"
                    )
                    page_error = True
                break

            if not page_error:
                if truncated:
                    # Fail CLOSED: we could not rule out a continuation hidden
                    # past a full page. Return a sentinel identity so the retry
                    # loop stops and the stale draft moves to manual review.
                    return {
                        "id": None,
                        "sentMessageId": None,
                        "internetMessageId": None,
                        "conversationId": conversation_id,
                        "subject": None,
                        "sentDateTime": None,
                        "recipientCount": 0,
                        "uncertainContinuation": True,
                        "reason": "sent_items_page_possibly_truncated",
                    }
                return None
        except Exception as exc:
            last_error = exc
            print(f"   ⚠️ Sent Items manual continuation guard lookup failed: {exc}")

        if attempt < attempts - 1:
            import time

            time.sleep(0.5 * (attempt + 1))

    if last_error:
        raise SentMailGuardLookupError(str(last_error))
    return None


def find_sent_recipient_continuation_for_retry(
    headers: Dict[str, str],
    *,
    recipient: str,
    sent_after: Optional[datetime],
    base: str = "https://graph.microsoft.com/v1.0",
    attempts: int = 2,
) -> Optional[Dict[str, Any]]:
    """Find any newer manual send to a recipient, even in a new conversation.

    Exact operator recovery cannot assume a user continued through Outlook's
    reply button. This metadata-only guard catches a fresh compose to the same
    broker address without reading message bodies.
    """
    recipient = _normalize_email(recipient)
    sent_after_utc = coerce_utc_datetime(sent_after)
    if not recipient or not sent_after_utc:
        raise SentMailGuardLookupError(
            "recipient and sent_after are required for Sent Items recipient guard"
        )

    params = {
        "$orderby": "sentDateTime desc",
        "$top": "25",
        "$select": (
            "id,internetMessageId,conversationId,subject,toRecipients,"
            "ccRecipients,bccRecipients,sentDateTime"
        ),
        "$filter": (
            "sentDateTime ge "
            f"{sent_after_utc.isoformat().replace('+00:00', 'Z')}"
        ),
    }

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            url = f"{base}/me/mailFolders/SentItems/messages"
            request_params: Optional[Dict[str, str]] = params
            while True:
                response = exponential_backoff_request(
                    lambda u=url, p=request_params: requests.get(
                        u,
                        headers=headers,
                        params=p,
                        timeout=30,
                    )
                )
                if response.status_code != 200:
                    last_error = RuntimeError(
                        f"Sent Items recipient lookup returned HTTP {response.status_code}"
                    )
                    break

                payload = response.json() or {}
                for message in payload.get("value", []) or []:
                    if recipient not in _message_recipients(message):
                        continue
                    sent_time = coerce_utc_datetime(message.get("sentDateTime"))
                    if sent_time and sent_time < sent_after_utc:
                        continue
                    return _message_identity(message)

                next_link = payload.get("@odata.nextLink")
                if not next_link:
                    return None
                if not _same_graph_origin(base, next_link):
                    last_error = RuntimeError(
                        "Unexpected @odata.nextLink host from Graph API"
                    )
                    break
                url = next_link
                request_params = None
        except Exception as exc:
            last_error = exc
            print(f"   ⚠️ Sent Items recipient continuation lookup failed: {exc}")

        if attempt < attempts - 1:
            import time

            time.sleep(0.5 * (attempt + 1))

    if last_error:
        raise SentMailGuardLookupError(str(last_error))
    return None
