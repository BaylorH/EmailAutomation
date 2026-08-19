"""Who a certification request is actually from.

Internal ingress and IAM decide who can reach the port. This decides who the
request is FROM, and only the second answer ends up bound into a stamp. A
verdict that cannot name its caller is a verdict nobody is accountable for.

Every check here is a refusal, never a normalisation. There is no "close
enough" issuer and no defaulted audience: an unconfigured verifier fails closed
rather than degrading into an open door, which is the failure mode that makes
authentication code worse than none.

Signature verification is INJECTED. The default decoder is Google's, which
fetches signing certificates over the network; injecting it lets the hostile
cases run offline under the blocked-socket check without ever skipping the
check itself. A decoder that raises is a rejection, not a pass.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

# Google issues ID tokens under both spellings. Both are exact matches; neither
# is a prefix test, because "https://accounts.google.com.evil.invalid" starts
# with a legitimate issuer.
ACCEPTED_ISSUERS = frozenset({
    "https://accounts.google.com",
    "accounts.google.com",
})

# Domain-separated so an operator digest can never collide with a digest of the
# same bytes computed for some other purpose.
_DIGEST_PREFIX = b"sitesift-certification-caller-v1\x1f"


class CallerRejected(PermissionError):
    """A refused caller. Names the FIELD that failed, never the value presented.

    Echoing the presented value would turn the service log into a durable record
    of whatever an unauthenticated caller chose to send.
    """


@dataclass(frozen=True)
class CallerIdentity:
    """The verified caller, reduced to what may be persisted."""

    subject: str
    digest: str


def _reject(field_name: str) -> None:
    raise CallerRejected(f"caller {field_name} did not match the expected operator")


def default_decoder(token: str, audience: str) -> Mapping[str, Any]:
    """Verify signature and audience with Google's own verifier."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


def caller_digest(subject: str, email: str) -> str:
    """Bind BOTH the numeric subject and the address.

    The subject alone is the durable identity, but including the address means a
    stamp distinguishes two principals even in the pathological case where a
    provider reuses a subject.
    """
    material = _DIGEST_PREFIX + f"{subject}\x1f{email}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def verify_caller(
    token: str,
    *,
    expected_audience: str,
    expected_email: str,
    expected_sub: str,
    decoder: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
    now_epoch: Optional[int] = None,
) -> CallerIdentity:
    """Verify one Google OIDC token against the exact expected operator."""
    # Configuration first. An unconfigured verifier must refuse everyone rather
    # than accept anyone.
    if not expected_audience or not expected_email or not expected_sub:
        raise CallerRejected("caller verification is not configured")
    if not token:
        _reject("token")

    decode = decoder or default_decoder
    try:
        claims = decode(token, expected_audience)
    except CallerRejected:
        raise
    except Exception:      # noqa: BLE001 - an unverifiable token is a refusal
        # The underlying reason is deliberately discarded: it is attacker-shaped
        # text, and it is not needed to act on the refusal.
        raise CallerRejected("caller token could not be verified") from None

    if not isinstance(claims, Mapping):
        _reject("token")

    if claims.get("iss") not in ACCEPTED_ISSUERS:
        _reject("issuer")

    # Re-checked here even though the decoder was given the audience: a custom
    # or future decoder that ignored the argument would otherwise silently
    # remove this check.
    if claims.get("aud") != expected_audience:
        _reject("audience")

    email = claims.get("email")
    if not isinstance(email, str) or not email:
        _reject("email")
    if email != expected_email:
        _reject("email")

    # Exactly the boolean True. The string "true" is not a verified email, and
    # accepting truthy values would let "false" through as well.
    if claims.get("email_verified") is not True:
        _reject("email_verified")

    subject = claims.get("sub")
    if not isinstance(subject, str) or subject != expected_sub:
        # An address can be reassigned to a new principal; the numeric subject
        # cannot. Checking only the address would accept a recreated account.
        _reject("subject")

    expiry = claims.get("exp")
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        _reject("expiry")
    if expiry <= (now_epoch if now_epoch is not None else int(time.time())):
        _reject("expiry")

    return CallerIdentity(subject=subject, digest=caller_digest(subject, email))
