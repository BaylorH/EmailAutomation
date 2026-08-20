#!/usr/bin/env python3
"""Can this system email anyone right now, and who?

Run BEFORE and AFTER every live test. Exit 0 = no send exposure outside the
allow-list. Exit 1 = something could send, or could send to someone external.

    cd <repo>
    set -a; source .env; set +a
    GOOGLE_APPLICATION_CREDENTIALS=$PWD/service-account.json \
        python3 scripts/audit_send_exposure.py

Pass client ids as arguments to declare campaigns that are EXPECTED to be live for
the test in flight; every other live client is reported as exposure.

Why this lives in the repo. It is the check that made the last live session safe to
run, and until now it existed only in a session scratch directory -- a guardrail
that evaporates between sessions is one nobody can rely on, and the run it is
missing from is exactly the run that needs it. It reads Firestore and sends
nothing.

What it looks at, across ALL users rather than the one the dashboard happens to
show -- other mailboxes hold real third-party broker correspondence:
  * live (non-terminal) clients, which are campaigns that can still send
  * unsent outbox items
  * queued pending responses
  * threads with a follow-up armed, checked against the allow-list
"""
import sys
from typing import Iterable, Sequence

# The ONLY addresses this system may contact during testing.
ALLOWED_EXACT = frozenset({"baylor.freelance@outlook.com", "baylor@manifoldengineering.ai"})
ALLOWED_PREFIX = "bp21harrison"
ALLOWED_DOMAIN = "@gmail.com"

TERMINAL_CLIENT = frozenset({"stopped", "completed", "archived", "deleted"})
TERMINAL_OUTBOX = frozenset({"sent", "cancelled", "canceled", "failed", "blocked"})


def allowed(email: str) -> bool:
    """Is this address one of Baylor's own test addresses?

    An empty address is allowed because it is not a destination -- absence cannot
    receive mail. Anything else must be an exact self-owned address or a plus-alias
    on the self-owned consumer account. Substring matching is deliberately avoided:
    'bp21harrison@gmail.com.attacker.net' must NOT pass, which is why the domain is
    checked with endswith rather than 'in'.
    """
    address = (email or "").strip().lower()
    if not address:
        return True
    if address in ALLOWED_EXACT:
        return True
    return address.startswith(ALLOWED_PREFIX) and address.endswith(ALLOWED_DOMAIN)


def audit(fs, expect_live: Iterable[str] = ()) -> "tuple[list[str], list[str]]":
    """Return (problems, notes). Pure over the Firestore client it is handed."""
    expected = set(expect_live or ())
    problems: list[str] = []
    notes: list[str] = []

    for user in fs.collection("users").stream():
        uid = user.id
        base = fs.collection("users").document(uid)

        for client in base.collection("clients").stream():
            data = client.to_dict() or {}
            status = str(data.get("status") or "").strip().lower()
            if status in TERMINAL_CLIENT:
                continue
            name = data.get("name", "?")
            if client.id in expected:
                notes.append(f"LIVE (expected): {name!r} [{uid[:10]}]")
            else:
                problems.append(
                    f"LIVE CLIENT not in allow-list: {name!r} status={status} "
                    f"id={client.id} user={uid[:10]}"
                )

        pending = [
            item for item in base.collection("outbox").stream()
            if str((item.to_dict() or {}).get("status") or "").lower() not in TERMINAL_OUTBOX
        ]
        if pending:
            problems.append(f"OUTBOX has {len(pending)} unsent item(s) for user {uid[:10]}")

        queued = list(base.collection("pendingResponses").stream())
        if queued:
            problems.append(f"{len(queued)} pendingResponses for user {uid[:10]}")

        armed_external, armed_ok = 0, 0
        for thread in base.collection("threads").where("followUpStatus", "==", "waiting").stream():
            data = thread.to_dict() or {}
            addresses: Sequence[str] = data.get("email") or []
            if isinstance(addresses, str):
                addresses = [addresses]
            if any(not allowed(address) for address in addresses):
                armed_external += 1
            else:
                armed_ok += 1
        if armed_external:
            problems.append(
                f"{armed_external} follow-up ARMED thread(s) with EXTERNAL recipients, "
                f"user {uid[:10]}"
            )
        if armed_ok:
            notes.append(
                f"{armed_ok} follow-up armed thread(s), allow-listed recipients, "
                f"user {uid[:10]}"
            )

    return problems, notes


def main(expect_live: Iterable[str] = ()) -> int:
    from google.cloud import firestore

    problems, notes = audit(firestore.Client(), expect_live)

    print("=== SEND EXPOSURE AUDIT ===")
    for note in notes:
        print("  note:", note)
    if problems:
        print("\nEXPOSURE FOUND:")
        for problem in problems:
            print("  !!", problem)
        return 1
    print("  clean: nothing can send, and nothing is armed for a non-allow-listed address")
    return 0


if __name__ == "__main__":
    sys.exit(main(expect_live=set(sys.argv[1:])))
