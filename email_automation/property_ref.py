"""A minted identity for a property, so no layer has to re-derive one.

Every layer of this system answers "which property is this?" at the point of
use, with its own private rule, from `get_row_anchor(rowvals, header)` output --
a DISPLAY STRING assembled out of two live sheet cells. That is not an identity,
and the 2026-08-06 production campaign proved it twice (PROD-0806-1, -3). Both
were fixed one at a time by putting the anchor into the thing that had been
missing a property; neither fixed the reason the anchor cannot carry that job:

  * `get_row_anchor` returns the literal `"Row data incomplete"` when it cannot
    read the address and city cells, and `"Unknown property"` when it raises.
    Both are non-empty strings, so every consumer that only guards `if not
    anchor` accepts them as a property NAME. Two different unreadable properties
    then share one canonical identity -- which is the suppressed-tour defect
    exactly, re-armed one level down.
  * The two sentinels also differ from each other, so a single property that is
    unreadable once and raises once is billed as two properties.
  * The anchor is rebuilt from cells the customer edits. Correcting a misspelled
    street or filling in a blank city cell changes the anchor, and therefore
    changes anything keyed on it, for a property that never moved.

This module mints the identity instead. It is deliberately a LEAF: no intra-
package imports, so any layer can adopt it without an import cycle, and so it
stays testable without provisioning the app's environment.

The mint is content-derived rather than random because it must be reproducible
by a caller that has no store to read -- and today there is no store to read.
The durable form is an opaque id written once into a hidden sheet column, which
mutates a customer-owned spreadsheet and is therefore Baylor's call, not an
agent's. Until that runs, `resolve_property_ref` prefers a ref already recorded
on the thread document (Firestore, ours) over re-minting, which is what makes
the ref survive a cosmetic anchor edit mid-campaign.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

# What `get_row_anchor` returns when it CANNOT name the property. These are
# error values wearing a name's clothes; the whole bug is consumers reading them
# as names. Compared normalized, so "unknown property" and "Unknown Property"
# are both caught.
NON_IDENTIFYING_ANCHORS = frozenset({
    "row data incomplete",
    "unknown property",
})

_IDENTIFIED_PREFIX = "prop"
_PROVISIONAL_PREFIX = "row"
_DIGEST_CHARS = 16
_FIELD_SEP = "\x1f"


def normalize_anchor(anchor: Any) -> str:
    """Fold an anchor to its comparison form.

    Identical to `ai_processing._normalize_ai_meta_anchor`, which has normalized
    anchors this way for AI_META drift detection all along -- that function now
    delegates here so the codebase has ONE answer to "are these the same
    property?" rather than one per call site.
    """
    return " ".join(str(anchor or "").strip().lower().replace(",", " ").split())


def is_identifying_anchor(anchor: Any) -> bool:
    """True when the anchor actually names a property.

    Guard with this instead of `if not anchor`: the sentinels are truthy, and
    that truthiness is what lets two properties share one key.
    """
    normalized = normalize_anchor(anchor)
    if not normalized:
        return False
    return normalized not in NON_IDENTIFYING_ANCHORS


def _digest(*parts: Any) -> str:
    joined = _FIELD_SEP.join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def mint_property_ref(
    *,
    client_id: Any = None,
    row_anchor: Any = None,
    thread_id: Any = None,
    row_number: Any = None,
) -> str:
    """Mint a ref for one property inside one campaign.

    Scoped by `client_id` because the scripts, the sheet and the ordinals are
    all per campaign -- the same address in two campaigns is two rows with two
    separate lives, and conflating them is how PROD-0806-1 spent a contact
    ordinal as a property ordinal.

    Two forms, and the difference is honest rather than cosmetic:

    `prop_*` -- minted from the normalized anchor. Stable for the property's
    whole life as long as the address and city keep describing the same place,
    and unmoved by case, spacing or a trailing comma.

    `row_*` -- minted from thread and row position, used ONLY when the anchor
    identifies nothing. It is provisional: it changes if an unidentifiable row
    is moved or re-sorted, and the failure that causes is a DUPLICATE
    notification. That is the direction this code already chose deliberately
    when the event key became property-scoped -- a duplicate is visible and
    recoverable, a suppressed notification is invisible. It is strictly better
    than today, where every unidentifiable property in the campaign collapses
    onto the single string "Row data incomplete" and only the first one of them
    is ever seen. The non-provisional replacement is the hidden ref column, and
    that waits on Baylor.

    Returns "" when there is nothing at all to mint from; callers must treat an
    empty ref as "no identity available" and fall back, never as an identity.
    """
    if is_identifying_anchor(row_anchor):
        return f"{_IDENTIFIED_PREFIX}_{_digest(client_id, normalize_anchor(row_anchor))}"

    position = "" if row_number is None else str(row_number).strip()
    if position:
        return f"{_PROVISIONAL_PREFIX}_{_digest(client_id, thread_id, position)}"

    return ""


def is_provisional_ref(ref: Any) -> bool:
    """True for a positionally-minted ref, which does not survive a row move."""
    return str(ref or "").startswith(f"{_PROVISIONAL_PREFIX}_")


def resolve_property_ref(
    thread_data: Optional[dict],
    *,
    client_id: Any = None,
    row_anchor: Any = None,
    thread_id: Any = None,
    row_number: Any = None,
) -> str:
    """The ref this thread's CURRENT property should be known by.

    A thread SURVIVES property replacement -- when the original goes unavailable
    and a replacement row is inserted, the same thread comes to be about a
    different property. So the answer is not simply "whatever was stored"; it is:

      * a stored ref still describing the same anchor -> keep it, so a cosmetic
        anchor edit cannot re-key events that were already handled;
      * a stored ref whose anchor has been REPLACED by a different identifying
        anchor -> mint a new ref, because this is a different property and its
        events must not land on the old one's already-handled entries;
      * a stored ref while the anchor has gone unreadable -> keep it, because a
        transiently unreadable row must not lose the identity it had;
      * nothing stored -> mint.
    """
    stored = str((thread_data or {}).get("propertyRef") or "").strip()
    stored_anchor = (thread_data or {}).get("propertyRefAnchor")

    if stored:
        if not is_identifying_anchor(row_anchor):
            return stored
        if not is_identifying_anchor(stored_anchor):
            # Stored under a sentinel and now readable: the row finally has a
            # name, so upgrade off the provisional ref onto the durable one.
            return mint_property_ref(
                client_id=client_id, row_anchor=row_anchor,
                thread_id=thread_id, row_number=row_number,
            )
        if normalize_anchor(stored_anchor) == normalize_anchor(row_anchor):
            return stored
        # Different identifying anchor: the thread has moved to another property.
        return mint_property_ref(
            client_id=client_id, row_anchor=row_anchor,
            thread_id=thread_id, row_number=row_number,
        )

    return mint_property_ref(
        client_id=client_id, row_anchor=row_anchor,
        thread_id=thread_id, row_number=row_number,
    )
