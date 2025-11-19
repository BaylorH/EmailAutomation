from typing import Optional, Dict, Any
from datetime import datetime, timezone
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import _fs
from .utils import b64url_id

def save_thread_root(user_id: str, root_id: str, meta: Dict[str, Any]):
    """Save or update thread root document."""
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(root_id)
        meta["updatedAt"] = SERVER_TIMESTAMP
        if "createdAt" not in meta:
            meta["createdAt"] = SERVER_TIMESTAMP
        
        thread_ref.set(meta, merge=True)
        print(f"💾 Saved thread root: {root_id}")
    except Exception as e:
        print(f"❌ Failed to save thread root {root_id}: {e}")

def save_message(user_id: str, thread_id: str, message_id: str, payload: Dict[str, Any]):
    """Save message to thread."""
    try:
        msg_ref = (_fs.collection("users").document(user_id)
                   .collection("threads").document(thread_id)
                   .collection("messages").document(message_id))
        payload["createdAt"] = SERVER_TIMESTAMP
        msg_ref.set(payload, merge=True)
        print(f"💾 Saved message {message_id} to thread {thread_id}")
    except Exception as e:
        print(f"❌ Failed to save message {message_id}: {e}")

def index_message_id(user_id: str, message_id: str, thread_id: str):
    """Index message ID for O(1) lookup."""
    try:
        encoded_id = b64url_id(message_id)
        index_ref = _fs.collection("users").document(user_id).collection("msgIndex").document(encoded_id)
        index_ref.set({"threadId": thread_id}, merge=True)
        print(f"🔍 Indexed message ID: {message_id[:50]}... -> {thread_id}")
    except Exception as e:
        print(f"❌ Failed to index message {message_id}: {e}")

def lookup_thread_by_message_id(user_id: str, message_id: str) -> Optional[str]:
    """Look up thread ID by message ID."""
    try:
        encoded_id = b64url_id(message_id)
        doc = _fs.collection("users").document(user_id).collection("msgIndex").document(encoded_id).get()
        if doc.exists:
            return doc.to_dict().get("threadId")
        return None
    except Exception as e:
        print(f"❌ Failed to lookup message {message_id}: {e}")
        return None

def index_conversation_id(user_id: str, conversation_id: str, thread_id: str):
    """Index conversation ID for fallback lookup."""
    if not conversation_id:
        return
    try:
        conv_ref = _fs.collection("users").document(user_id).collection("convIndex").document(conversation_id)
        conv_ref.set({"threadId": thread_id}, merge=True)
        print(f"🔍 Indexed conversation ID: {conversation_id} -> {thread_id}")
    except Exception as e:
        print(f"❌ Failed to index conversation {conversation_id}: {e}")

def lookup_thread_by_conversation_id(user_id: str, conversation_id: str) -> Optional[str]:
    """Look up thread ID by conversation ID (fallback)."""
    if not conversation_id:
        return None
    try:
        doc = _fs.collection("users").document(user_id).collection("convIndex").document(conversation_id).get()
        if doc.exists:
            return doc.to_dict().get("threadId")
        return None
    except Exception as e:
        print(f"❌ Failed to lookup conversation {conversation_id}: {e}")
        return None

def _get_thread_messages_chronological(uid: str, thread_id: str) -> list[dict]:
    """Get all messages in thread in chronological order."""
    try:
        messages_ref = (_fs.collection("users").document(uid)
                        .collection("threads").document(thread_id)
                        .collection("messages"))
        messages = list(messages_ref.stream())
        
        if not messages:
            return []
        
        # Sort by timestamp
        message_data = []
        for msg in messages:
            data = msg.to_dict()
            # Use sentDateTime for outbound, receivedDateTime for inbound
            timestamp = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(timestamp, 'timestamp'):
                timestamp = timestamp.timestamp()
            elif isinstance(timestamp, str):
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    timestamp = dt.timestamp()
                except:
                    timestamp = 0
            else:
                timestamp = 0
                
            message_data.append((timestamp, data, msg.id))
        
        message_data.sort(key=lambda x: x[0])
        return [{"data": data, "id": msg_id} for _, data, msg_id in message_data]
        
    except Exception as e:
        print(f"❌ Failed to get thread messages: {e}")
        return []

def build_conversation_payload(uid: str, thread_id: str, limit: int = 10, headers: dict = None) -> list[dict]:
    """
    Return last N messages in chronological order. Each item includes:
    direction, from, to, subject, timestamp, preview (short), content (full text, bounded)
    
    Fetches from both Firestore (indexed messages) and Microsoft Graph API (all messages in thread)
    to include manual emails that weren't indexed (e.g., Jill's manual replies).
    """
    try:
        # Get messages from Firestore (what we've indexed)
        firestore_messages = _get_thread_messages_chronological(uid, thread_id)
        
        # Also fetch from Graph API if headers provided and we have conversationId
        graph_messages = []
        if headers:
            try:
                # Get conversationId from thread metadata
                thread_ref = _fs.collection("users").document(uid).collection("threads").document(thread_id)
                thread_doc = thread_ref.get()
                if thread_doc.exists:
                    thread_data = thread_doc.to_dict()
                    conversation_id = thread_data.get("conversationId")
                    
                    if conversation_id:
                        # Fetch all messages in this conversation from Graph API
                        # This includes messages we didn't index (e.g., Jill's manual emails)
                        import requests
                        from .utils import exponential_backoff_request
                        from .utils import strip_html_tags
                        
                        try:
                            response = exponential_backoff_request(
                                lambda: requests.get(
                                    "https://graph.microsoft.com/v1.0/me/messages",
                                    headers=headers,
                                    params={
                                        "$filter": f"conversationId eq '{conversation_id}'",
                                        "$orderby": "sentDateTime asc",
                                        "$select": "id,subject,from,toRecipients,sentDateTime,receivedDateTime,body,bodyPreview,internetMessageId",
                                        "$top": 50  # Limit to prevent huge responses
                                    },
                                    timeout=30
                                )
                            )
                            
                            if response.status_code == 200:
                                graph_data = response.json()
                                for msg in graph_data.get("value", []):
                                    # Determine direction
                                    from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
                                    to_recipients = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
                                    
                                    # Determine direction: if receivedDateTime exists, it's inbound; if only sentDateTime, likely outbound
                                    # Also check if message is in SentItems folder (we'd need to fetch that separately)
                                    # For now, use heuristic: if receivedDateTime exists and no sentDateTime, it's inbound
                                    # If sentDateTime exists but no receivedDateTime, it's outbound
                                    sent_dt = msg.get("sentDateTime")
                                    received_dt = msg.get("receivedDateTime")
                                    
                                    if received_dt and not sent_dt:
                                        direction = "inbound"
                                    elif sent_dt and not received_dt:
                                        direction = "outbound"
                                    else:
                                        # Both exist or neither - default to inbound (most common case)
                                        direction = "inbound"
                                    
                                    # Get body content
                                    body_obj = msg.get("body", {}) or {}
                                    body_content = body_obj.get("content", "")
                                    body_type = body_obj.get("contentType", "Text")
                                    if body_type == "HTML":
                                        body_content = strip_html_tags(body_content)
                                    
                                    graph_messages.append({
                                        "data": {
                                            "direction": direction,
                                            "from": from_addr,
                                            "to": to_recipients,
                                            "subject": msg.get("subject", ""),
                                            "sentDateTime": msg.get("sentDateTime"),
                                            "receivedDateTime": msg.get("receivedDateTime"),
                                            "body": {
                                                "content": body_content,
                                                "preview": msg.get("bodyPreview", "")[:200]
                                            },
                                            "internetMessageId": msg.get("internetMessageId")
                                        },
                                        "id": msg.get("internetMessageId") or msg.get("id")
                                    })
                                    
                                print(f"📧 Fetched {len(graph_messages)} messages from Graph API for conversation {conversation_id}")
                        except Exception as e:
                            print(f"⚠️ Failed to fetch messages from Graph API: {e}")
            except Exception as e:
                print(f"⚠️ Failed to fetch Graph messages: {e}")
        
        # Merge messages from both sources, deduplicate by internetMessageId
        all_messages = {}
        
        # Add Firestore messages
        for msg_info in firestore_messages:
            msg_id = msg_info.get("id") or ""
            all_messages[msg_id] = msg_info
        
        # Add Graph messages (will overwrite Firestore if duplicate, but that's fine)
        for msg_info in graph_messages:
            msg_id = msg_info.get("id") or ""
            if msg_id not in all_messages:  # Only add if not already in Firestore
                all_messages[msg_id] = msg_info
        
        # Convert to list and sort chronologically
        messages_list = list(all_messages.values())
        
        # Sort by timestamp
        message_data = []
        for msg_info in messages_list:
            data = msg_info["data"]
            ts = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(ts, 'timestamp'):
                ts = ts.timestamp()
            elif isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    ts = dt.timestamp()
                except:
                    ts = 0
            else:
                ts = 0
            message_data.append((ts, msg_info))
        
        message_data.sort(key=lambda x: x[0])
        sorted_messages = [msg_info for _, msg_info in message_data]
        
        # Take last N messages
        recent = sorted_messages[-limit:] if len(sorted_messages) > limit else sorted_messages

        payload = []
        CUT = 2000  # cap to keep prompt small but meaningful
        for msg_info in recent:
            data = msg_info["data"]

            ts = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            elif not isinstance(ts, str):
                ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            body = data.get("body", {}) or {}
            full_text = (body.get("content") or "")[:CUT]
            preview = (body.get("preview") or "")[:200]

            payload.append({
                "direction": data.get("direction", "unknown"),
                "from": data.get("from", ""),
                "to": data.get("to", []),
                "subject": data.get("subject", ""),
                "timestamp": ts,
                "preview": preview,
                "content": full_text,
            })

        return payload
    except Exception as e:
        print(f"❌ Failed to build conversation payload: {e}")
        return []

def dump_thread_from_firestore(user_id: str, thread_id: str):
    """Console dump of thread conversation in chronological order."""
    try:
        print(f"\n📜 CONVERSATION THREAD: {thread_id}")
        print("=" * 80)
        
        # Get all messages in thread
        messages_ref = (_fs.collection("users").document(user_id)
                        .collection("threads").document(thread_id)
                        .collection("messages"))
        messages = list(messages_ref.stream())
        
        if not messages:
            print("(No messages found)")
            return
        
        # Sort by timestamp
        message_data = []
        for msg in messages:
            data = msg.to_dict()
            # Use sentDateTime for outbound, receivedDateTime for inbound
            timestamp = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(timestamp, 'timestamp'):
                timestamp = timestamp.timestamp()
            message_data.append((timestamp, data))
        
        message_data.sort(key=lambda x: x[0] if x[0] else 0)
        
        for timestamp, data in message_data:
            direction = data.get("direction", "unknown")
            subject = data.get("subject", "")
            from_addr = data.get("from", "")
            to_addrs = data.get("to", [])
            preview = data.get("body", {}).get("preview", "")
            
            if direction == "outbound":
                arrow = "ME → " + ", ".join(to_addrs)
            else:
                arrow = f"{from_addr} → ME"
            
            print(f"{arrow}")
            print(f"   Subject: {subject}")
            print(f"   Preview: {preview}")
            print()
        
        print("=" * 80)
        
    except Exception as e:
        print(f"❌ Failed to dump thread {thread_id}: {e}")

def _processed_ref(user_id: str, key: str):
    """Get reference to processed message document."""
    encoded_key = b64url_id(key)
    return _fs.collection("users").document(user_id).collection("processedMessages").document(encoded_key)

def has_processed(user_id: str, key: str) -> bool:
    """Check if a message has already been processed."""
    try:
        doc = _processed_ref(user_id, key).get()
        return doc.exists
    except Exception as e:
        print(f"❌ Failed to check processed status for {key}: {e}")
        return False

def mark_processed(user_id: str, key: str):
    """Mark a message as processed."""
    try:
        _processed_ref(user_id, key).set({
            "processedAt": SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"❌ Failed to mark message as processed {key}: {e}")

def _sync_ref(user_id: str):
    """Get reference to sync document."""
    return _fs.collection("users").document(user_id).collection("sync").document("inbox")

def get_last_scan_iso(user_id: str) -> str | None:
    """Get the last scan timestamp."""
    try:
        doc = _sync_ref(user_id).get()
        if doc.exists:
            return doc.to_dict().get("lastScanISO")
        return None
    except Exception as e:
        print(f"❌ Failed to get last scan ISO: {e}")
        return None

def set_last_scan_iso(user_id: str, iso_str: str):
    """Set the last scan timestamp."""
    try:
        _sync_ref(user_id).set({
            "lastScanISO": iso_str,
            "updatedAt": SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"❌ Failed to set last scan ISO: {e}")