"""Sent Items reconciliation helpers for ambiguous Graph send failures.

Graph send endpoints can time out after Microsoft has accepted a message. Before
retrying a stored send, search the sender's Sent Items for a matching message so
we can stop duplicate sends and surface a reconciliation item instead.
"""

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, Optional

import requests

from .utils import exponential_backoff_request, strip_html_tags


class SentMailGuardLookupError(Exception):
    """Raised when Sent Items cannot be checked safely before a retry."""


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


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_text(value: Any) -> str:
    text = strip_html_tags(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _normalize_subject(value: Any) -> str:
    subject = _normalize_text(value)
    return re.sub(r"^((re|fw|fwd):\s*)+", "", subject)


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
                if conversation_id and message.get("conversationId") != conversation_id:
                    continue
                if recipient not in _message_recipients(message):
                    continue
                if not _subject_matches(subject, message):
                    continue
                if not _body_matches(body, message):
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
