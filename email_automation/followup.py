"""
Automatic Follow-Up Email System
================================

This module handles automatic follow-up emails when brokers don't respond
within configurable time periods.

Key features:
- 0-3 configurable follow-ups per thread
- Hours or days timing
- Pause/resume when broker responds then goes silent
- Sends as replies to maintain thread continuity

Called from main.py after inbox scanning.
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP

from .clients import _fs
from .utils import exponential_backoff_request


def check_and_send_followups(user_id: str, headers: Dict[str, str]) -> int:
    """
    Main entry point: scan threads needing follow-ups and send them.

    Called from main.py every 30 minutes.

    Returns: Number of follow-ups sent
    """
    print(f"\n{'='*60}")
    print("FOLLOW-UP CHECK")
    print(f"{'='*60}")

    now = datetime.now(timezone.utc)
    followups_sent = 0

    # Query threads with active follow-up tracking
    threads_ref = _fs.collection("users").document(user_id).collection("threads")

    # Find threads that are waiting for follow-up
    # Status must be 'waiting' and nextFollowUpAt must be in the past
    try:
        query = threads_ref.where("followUpStatus", "==", "waiting")
        waiting_threads = list(query.stream())
    except Exception as e:
        print(f"   Error querying follow-up threads: {e}")
        return 0

    if not waiting_threads:
        print("   No threads waiting for follow-up")
        return 0

    print(f"   Found {len(waiting_threads)} threads with follow-up tracking")

    for thread_doc in waiting_threads:
        thread_data = thread_doc.to_dict()
        thread_id = thread_doc.id

        followup_config = thread_data.get("followUpConfig", {})

        if not followup_config.get("enabled", False):
            continue

        next_followup_at = followup_config.get("nextFollowUpAt")
        if not next_followup_at:
            continue

        # Convert Firestore timestamp to datetime
        if hasattr(next_followup_at, 'timestamp'):
            next_followup_dt = datetime.fromtimestamp(
                next_followup_at.timestamp(),
                tz=timezone.utc
            )
        else:
            continue

        # Check if it's time for follow-up
        if now < next_followup_dt:
            time_remaining = next_followup_dt - now
            print(f"   Thread {thread_id[:20]}... - {time_remaining} until follow-up")
            continue

        # Check if broker has responded
        if thread_data.get("hasInboundReply", False):
            # Broker responded - pause the follow-up sequence
            _pause_followup(user_id, thread_id)
            continue

        # Get current follow-up index and messages
        current_index = followup_config.get("currentFollowUpIndex", 0)
        followups = followup_config.get("followUps", [])

        if current_index >= len(followups):
            # All follow-ups exhausted
            _mark_followup_complete(user_id, thread_id, "max_reached")
            continue

        # Send the follow-up
        success = _send_followup_email(
            user_id=user_id,
            headers=headers,
            thread_id=thread_id,
            thread_data=thread_data,
            followup_config=followup_config,
            followup_index=current_index
        )

        if success:
            followups_sent += 1

            # Schedule next follow-up if there are more
            _schedule_next_followup(
                user_id=user_id,
                thread_id=thread_id,
                followup_config=followup_config,
                just_sent_index=current_index
            )

    print(f"\n   Sent {followups_sent} follow-up email(s)")
    return followups_sent


def _send_followup_email(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    thread_data: Dict,
    followup_config: Dict,
    followup_index: int
) -> bool:
    """Send a follow-up email for a specific thread."""
    import requests

    try:
        followups = followup_config.get("followUps", [])
        if followup_index >= len(followups):
            return False

        followup = followups[followup_index]
        followup_message = followup.get("message", "")

        if not followup_message:
            followup_message = _get_default_followup_message(followup_index)

        recipient_emails = thread_data.get("email", [])
        if not recipient_emails:
            print(f"   No recipient email for thread {thread_id[:20]}...")
            return False

        recipient = recipient_emails[0] if isinstance(recipient_emails, list) else recipient_emails

        # Get the last outbound message to reply to
        messages_ref = (_fs.collection("users").document(user_id)
                       .collection("threads").document(thread_id)
                       .collection("messages"))

        try:
            outbound_messages = list(
                messages_ref.where("direction", "==", "outbound")
                .order_by("sentDateTime", direction="DESCENDING")
                .limit(1)
                .stream()
            )
        except Exception as e:
            # Index might not exist, try without order_by
            outbound_messages = [
                doc for doc in messages_ref.stream()
                if doc.to_dict().get("direction") == "outbound"
            ]
            if outbound_messages:
                outbound_messages = [outbound_messages[-1]]

        if not outbound_messages:
            print(f"   No outbound messages found in thread {thread_id[:20]}...")
            return False

        last_outbound = outbound_messages[0].to_dict()
        internet_msg_id = last_outbound.get("headers", {}).get("internetMessageId")

        # Find the Graph message ID
        base = "https://graph.microsoft.com/v1.0"

        if internet_msg_id:
            # Search by internetMessageId
            search_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages",
                    headers=headers,
                    params={
                        "$filter": f"internetMessageId eq '{internet_msg_id}'",
                        "$select": "id,subject,conversationId"
                    },
                    timeout=30
                )
            )

            if search_resp.status_code != 200:
                print(f"   Failed to find message: {search_resp.status_code}")
                return False

            messages = search_resp.json().get("value", [])
            if not messages:
                print(f"   Message not found in mailbox")
                return False

            graph_msg_id = messages[0]["id"]
            subject = messages[0].get("subject", thread_data.get("subject", "Follow-up"))
        else:
            print(f"   No internetMessageId for reply")
            return False

        # Personalize the message with contact name if available
        contact_name = thread_data.get("contactName", "")
        if contact_name and "[NAME]" in followup_message:
            first_name = contact_name.split()[0] if contact_name else ""
            followup_message = followup_message.replace("[NAME]", first_name)

        # Send as reply
        reply_body = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": followup_message.replace("\n", "<br>")
                }
            }
        }

        reply_resp = exponential_backoff_request(
            lambda: requests.post(
                f"{base}/me/messages/{graph_msg_id}/reply",
                headers=headers,
                json=reply_body,
                timeout=30
            )
        )

        if reply_resp.status_code in [200, 201, 202]:
            print(f"   Sent follow-up #{followup_index + 1} for thread {thread_id[:20]}...")

            # Update thread
            _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
                "lastOutboundAt": SERVER_TIMESTAMP,
                "updatedAt": SERVER_TIMESTAMP,
                "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP
            })

            return True
        else:
            print(f"   Failed to send follow-up: {reply_resp.status_code}")
            return False

    except Exception as e:
        print(f"   Error sending follow-up: {e}")
        return False


def _schedule_next_followup(
    user_id: str,
    thread_id: str,
    followup_config: Dict,
    just_sent_index: int
):
    """Schedule the next follow-up in the sequence."""
    followups = followup_config.get("followUps", [])
    next_index = just_sent_index + 1

    if next_index >= len(followups):
        # No more follow-ups
        _mark_followup_complete(user_id, thread_id, "max_reached")
        return

    # Calculate next follow-up time
    next_followup = followups[next_index]
    wait_time = next_followup.get("waitTime", 3)
    wait_unit = next_followup.get("waitUnit", "days")

    if wait_unit == "hours":
        delta = timedelta(hours=wait_time)
    else:
        delta = timedelta(days=wait_time)

    next_followup_at = datetime.now(timezone.utc) + delta

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpConfig.currentFollowUpIndex": next_index,
        "followUpConfig.nextFollowUpAt": next_followup_at,
        "followUpStatus": "waiting",
        "updatedAt": SERVER_TIMESTAMP
    })

    print(f"   Next follow-up scheduled for {next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC")


def _pause_followup(user_id: str, thread_id: str):
    """Pause follow-up sequence when broker responds."""
    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpStatus": "paused",
        "followUpConfig.pausedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })
    print(f"   Paused follow-up for thread {thread_id[:20]}... (broker responded)")


def _mark_followup_complete(user_id: str, thread_id: str, reason: str):
    """Mark follow-up sequence as complete."""
    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpStatus": reason,
        "updatedAt": SERVER_TIMESTAMP
    })
    print(f"   Follow-up sequence complete for thread {thread_id[:20]}... ({reason})")


def schedule_followup_for_thread(
    user_id: str,
    thread_id: str,
    followup_config: Dict
):
    """
    Schedule follow-ups for a newly sent thread.
    Called from email.py after sending initial outbound email.

    Args:
        user_id: Firebase user ID
        thread_id: Thread document ID
        followup_config: Configuration from outbox containing:
            - enabled: bool
            - followUps: [{waitTime, waitUnit, message}, ...]
    """
    if not followup_config or not followup_config.get("enabled", False):
        return

    followups = followup_config.get("followUps", [])
    if not followups:
        return

    # Calculate first follow-up time
    first_followup = followups[0]
    wait_time = first_followup.get("waitTime", 5)
    wait_unit = first_followup.get("waitUnit", "days")

    if wait_unit == "hours":
        delta = timedelta(hours=wait_time)
    else:
        delta = timedelta(days=wait_time)

    next_followup_at = datetime.now(timezone.utc) + delta

    # Update thread with follow-up config
    thread_followup_config = {
        "enabled": True,
        "followUps": followups,
        "currentFollowUpIndex": 0,
        "nextFollowUpAt": next_followup_at,
        "conversationStage": "initial",
        "pausedAt": None,
        "lastFollowUpSentAt": None
    }

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpConfig": thread_followup_config,
        "followUpStatus": "waiting",
        "hasInboundReply": False,
        "lastOutboundAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })

    print(f"   Follow-up scheduled: {wait_time} {wait_unit} ({next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC)")


def cancel_followup_on_response(user_id: str, thread_id: str):
    """
    Pause pending follow-up when broker responds.
    Called from processing.py when inbound message is detected.

    The sequence can resume if the broker goes silent again.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return

        thread_data = thread_doc.to_dict()
        followup_config = thread_data.get("followUpConfig", {})

        if not followup_config.get("enabled", False):
            return

        current_status = thread_data.get("followUpStatus")
        if current_status in ["paused", "completed", "max_reached"]:
            return

        thread_ref.update({
            "hasInboundReply": True,
            "lastInboundAt": SERVER_TIMESTAMP,
            "followUpStatus": "paused",
            "followUpConfig.pausedAt": SERVER_TIMESTAMP,
            "followUpConfig.conversationStage": "mid_conversation",
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Follow-up paused for thread {thread_id[:20]}... (broker responded)")

    except Exception as e:
        print(f"   Error pausing follow-up: {e}")


def resume_followup_if_silent(user_id: str, thread_id: str, silence_threshold_days: int = 3):
    """
    Resume follow-up sequence if broker went silent after responding.

    This is called to check paused threads and see if they should resume.
    Typically called from check_and_send_followups for paused threads.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return False

        thread_data = thread_doc.to_dict()

        if thread_data.get("followUpStatus") != "paused":
            return False

        last_inbound_at = thread_data.get("lastInboundAt")
        if not last_inbound_at:
            return False

        # Check if enough time has passed since last inbound
        if hasattr(last_inbound_at, 'timestamp'):
            last_inbound_dt = datetime.fromtimestamp(
                last_inbound_at.timestamp(),
                tz=timezone.utc
            )
        else:
            return False

        now = datetime.now(timezone.utc)
        silence_duration = now - last_inbound_dt

        if silence_duration < timedelta(days=silence_threshold_days):
            return False

        # Resume the sequence
        followup_config = thread_data.get("followUpConfig", {})
        current_index = followup_config.get("currentFollowUpIndex", 0)
        followups = followup_config.get("followUps", [])

        if current_index >= len(followups):
            return False

        # Calculate next follow-up time (immediate or short delay)
        next_followup = followups[current_index]
        wait_time = min(next_followup.get("waitTime", 1), 1)  # Cap at 1 day for resumed

        next_followup_at = now + timedelta(days=wait_time)

        thread_ref.update({
            "followUpStatus": "waiting",
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "hasInboundReply": False,  # Reset for next check
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Resumed follow-up for thread {thread_id[:20]}... (broker went silent)")
        return True

    except Exception as e:
        print(f"   Error resuming follow-up: {e}")
        return False


def _get_default_followup_message(index: int) -> str:
    """Return default follow-up message based on sequence position."""
    messages = [
        # Follow-up 1: Friendly reminder
        """Hi [NAME],

I wanted to follow up on my previous email regarding the property above. I understand you're busy, but I wanted to confirm whether this space might be a fit for my client's requirements.

If you could share the key specs (SF, asking rent, NNN, clear height, doors, power), that would be very helpful.

Thanks for your time!""",

        # Follow-up 2: Gentle nudge
        """Hi [NAME],

Just a quick check-in on my earlier emails about the property above. If you have a moment, I'd appreciate any details you can share.

If this property is no longer available or not a good fit, please let me know and I'll update my records.

Thank you!""",

        # Follow-up 3: Final attempt
        """Hi [NAME],

This will be my final follow-up regarding the property above. I'll assume this one isn't a fit for my client's needs, but if you'd like to discuss, I'm happy to connect.

If anything else comes available in the area that might work, please keep me in mind.

Thanks again for your time!"""
    ]

    if index < len(messages):
        return messages[index]
    return messages[-1]
