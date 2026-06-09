from __future__ import annotations


def _first_name(name_or_email: str) -> str:
    value = (name_or_email or "").strip()
    if not value:
        return ""
    if "@" in value:
        value = value.split("@", 1)[0].replace(".", " ").replace("_", " ")
    return value.split()[0].strip().title()


def build_wrong_contact_suggested_email(
    *,
    original_contact: str,
    suggested_contact: str,
    suggested_email: str,
    row_anchor: str | None,
    referrer_name: str | None,
) -> dict:
    """Build the InlineReplyComposer-compatible payload for wrong-contact referrals."""
    normalized_to = (suggested_email or "").strip().lower()
    recipient_name = _first_name(suggested_contact or normalized_to)
    referrer = (referrer_name or "").strip() or _first_name(original_contact) or "the previous contact"
    property_label = (row_anchor or "this property").strip()
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"

    return {
        "to": [normalized_to] if normalized_to else [],
        "subject": f"RE: {property_label}" if property_label else "RE: Property Inquiry",
        "contactName": (suggested_contact or "").strip() or recipient_name,
        "body": f"""{greeting}

{referrer} mentioned you might be the right contact for {property_label}.

Could you help confirm the current availability and property details?

Thanks!""",
    }


def build_new_property_suggested_email(
    *,
    address: str,
    city: str,
    to_email: str,
    contact_name: str | None,
    referrer_name: str | None,
    client_id: str,
) -> dict:
    """Build a fresh first-touch outreach for a broker-suggested replacement property."""
    normalized_to = (to_email or "").strip().lower()
    property_label = f"{address}, {city}" if city else address
    recipient_name = _first_name(contact_name or normalized_to)
    referrer = (referrer_name or "").strip()
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"

    if not referrer:
        body = f"""{greeting}

Thanks for sending over {property_label}.

Could you send any flyer or floor plans you have, and confirm any remaining property details such as total SF, asking rent, NNN/opex, loading, clear height, and power?

Thanks!"""
    else:
        body = f"""{greeting}

{referrer} mentioned you might be the right contact for {property_label}.

I'm helping a client review industrial/warehouse options in the area. Could you confirm availability and send the current property details, including total SF, asking rent, NNN/opex, loading, clear height, power, and any flyer or floor plans?

Thanks!"""

    return {
        "to": [normalized_to] if normalized_to else [],
        "subject": property_label,
        "contactName": (contact_name or "").strip() or recipient_name,
        "clientId": client_id,
        "rowNumber": None,
        "body": body,
    }


def should_skip_original_reply_for_new_property_referral(
    *,
    original_contact_email: str,
    new_property_email: str,
) -> bool:
    """Avoid asking the original broker to connect us when they already gave a new contact."""
    original = (original_contact_email or "").strip().lower()
    replacement = (new_property_email or "").strip().lower()
    return bool(original and replacement and original != replacement)


def sanitize_new_property_referral_response(
    proposal: dict,
    *,
    original_contact_email: str,
) -> dict:
    """Clear reply-back drafts when a new-property referral includes a different direct email."""
    if not isinstance(proposal, dict):
        return proposal

    sanitized = {
        **proposal,
        "events": [
            dict(event) if isinstance(event, dict) else event
            for event in (proposal.get("events") or [])
        ],
    }

    for event in sanitized["events"]:
        if not isinstance(event, dict) or event.get("type") != "new_property":
            continue

        if should_skip_original_reply_for_new_property_referral(
            original_contact_email=original_contact_email,
            new_property_email=event.get("email", ""),
        ):
            sanitized["response_email"] = None
            sanitized["skip_response"] = True
            break

    return sanitized
