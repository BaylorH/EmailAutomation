#!/usr/bin/env python3
"""Dry-run-first CLI for one exact Baylor/BP21 failed message replay."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from msal import ConfidentialClientApplication, SerializableTokenCache

from email_automation.operator_replay import (
    ReplayRefused,
    ReplayRequest,
    replay_exact_message,
    validate_approved_lane,
)
from firebase_helpers import download_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify or apply one exact failed Baylor/BP21 inbox replay."
    )
    parser.add_argument("--uid", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--graph-message-id", required=True)
    parser.add_argument("--internet-message-id", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--operator-recipient", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Process the exact verified message. Omit for read-only dry-run.",
    )
    return parser


def acquire_graph_headers(uid: str, expected_operator_recipient: str) -> dict:
    """Acquire one Graph bearer from the existing per-user MSAL cache.

    Token values and MSAL result payloads are never logged. Refreshed cache
    state is intentionally not uploaded by this one-shot operator command.
    """
    from email_automation.app_config import (
        AUTHORITY,
        CLIENT_ID,
        CLIENT_SECRET,
        FIREBASE_API_KEY,
        SCOPES,
    )

    if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
        raise ReplayRefused("Required Graph authentication configuration is unavailable")

    with tempfile.TemporaryDirectory(prefix="sitesift-replay-auth-") as temp_dir:
        cache_path = os.path.join(temp_dir, "msal_token_cache.bin")
        download_token(
            FIREBASE_API_KEY,
            output_file=cache_path,
            user_id=uid,
        )
        if not os.path.isfile(cache_path) or os.path.getsize(cache_path) == 0:
            raise ReplayRefused("Existing per-user MSAL cache could not be loaded")

        with open(cache_path, "r", encoding="utf-8") as cache_file:
            serialized_cache = cache_file.read()

        cache = SerializableTokenCache()
        cache.deserialize(serialized_cache)
        app = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache,
        )
        expected_email = expected_operator_recipient.strip().lower()
        accounts = [
            account
            for account in app.get_accounts()
            if str((account or {}).get("username") or "").strip().lower()
            == expected_email
        ]
        if len(accounts) != 1:
            raise ReplayRefused(
                "Existing per-user MSAL cache does not contain exactly one matching operator account"
            )

        token_result = app.acquire_token_silent(SCOPES, account=accounts[0])
        access_token = (
            token_result.get("access_token")
            if isinstance(token_result, dict)
            else None
        )
        if not access_token:
            raise ReplayRefused(
                "Graph access token could not be acquired from the existing per-user cache"
            )

    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _request_from_args(args: argparse.Namespace) -> ReplayRequest:
    return ReplayRequest(
        uid=args.uid,
        client_id=args.client_id,
        thread_id=args.thread_id,
        graph_message_id=args.graph_message_id,
        internet_message_id=args.internet_message_id,
        sender=args.sender,
        operator_recipient=args.operator_recipient,
    )


def _safe_result_summary(result) -> dict:
    data = result.to_dict()
    data.pop("sender", None)
    data.pop("operator_recipient", None)
    data["approved_lane"] = "baylor_bp21"
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    request = _request_from_args(args)

    try:
        # Refuse unsafe identities before touching the per-user token cache.
        validate_approved_lane(request)
        headers = acquire_graph_headers(request.uid, request.operator_recipient)
        result = replay_exact_message(
            request,
            headers,
            apply=args.apply,
        )
    except ReplayRefused as exc:
        print(f"Replay refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Do not print exception payloads: auth/network libraries can include
        # sensitive request context in their error text.
        print(
            f"Replay failed safely ({type(exc).__name__}); no token details were emitted.",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: exact-message replay {result.status}")
    print(json.dumps(_safe_result_summary(result), sort_keys=True))
    if not args.apply:
        print(
            "No message or campaign state mutation performed; the user lease was "
            "acquired and released. Re-run with --apply only after reviewing this preflight."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
