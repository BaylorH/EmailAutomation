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
        "body": f"""{greeting}

{referrer} mentioned you might be the right contact for {property_label}.

Could you help confirm the current availability and property details?

Thanks!""",
    }
