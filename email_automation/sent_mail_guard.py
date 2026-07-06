"""Sent Items reconciliation helpers for ambiguous Graph send failures.

Graph send endpoints can time out after Microsoft has accepted a message. Before
retrying a stored send, search the sender's Sent Items for a matching message so
we can stop duplicate sends and surface a reconciliation item instead.
"""

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

from .utils import exponential_backoff_request, strip_html_tags


class SentMailGuardLookupError(Exception):
    """Raised when Sent Items cannot be checked safely before a retry."""


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
