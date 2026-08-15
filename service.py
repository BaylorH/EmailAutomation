"""HTTP service entrypoint for the EmailAutomation per-user pipeline (Phase-1
webhook migration).

This wraps the EXISTING per-user pipeline — ``main.refresh_and_process_user`` —
as a minimal Flask app so a queue (Cloud Tasks) can drive one user per HTTP
request instead of the whole-batch Cloud Run Job / GitHub Actions cron. It is
FUNCTIONALITY-NEUTRAL: the endpoint changes only *how* the pipeline is invoked
(one user, on demand, under a per-user lease), never *what* the pipeline does.

Routes
------
POST /process-user   body {"uid": "<firebase-uid>"}
    Runs ``run_with_user_lease(uid, lambda: refresh_and_process_user(uid))``.
      * 200 {"status": "processed"}       — lease acquired, pipeline ran
      * 503 {"status": "skipped_locked"}  — user already being processed;
        retry so work created after the active worker's snapshot is not stranded
      * 400 {"status": "error", ...}      — missing / blank uid or non-JSON body
      * 401 {"status": "error", ...}      — auth required and missing/wrong secret
      * 500 {"status": "error", "error"}  — pipeline raised (so Cloud Tasks retries)
POST /process-outbox body {"uid": "<firebase-uid>", "outboxId": "<document-id>"}
    Continues only that exact outbox document under the same per-user lease.
    Reviewed results are bounded to processed/terminal/manual-review/invalid;
    Task 8 itself leaves a ready item fail-closed for the Task 9 sender.
GET  /health         — Cloud Run-safe liveness probe, always 200
GET  /healthz        — legacy liveness alias, always 200 (never auth-gated)

Auth
----
Optional shared-secret gate via the ``PROCESS_USER_AUTH`` env var. When set,
every /process-user request must present the secret as either
``Authorization: Bearer <secret>`` or ``X-Process-User-Auth: <secret>``
(constant-time compared). When unset, the endpoint is open — acceptable behind
Cloud Run's own IAM/ingress but you should set it before exposing publicly.

TODO(auth): replace/augment the shared secret with real OIDC ID-token
verification for the Cloud Tasks -> Cloud Run OIDC invoker. Cloud Run itself can
enforce the OIDC audience at the platform layer ("require authentication" +
Tasks OIDC token), so the heavy JWT signature/issuer/audience verification is
intentionally deferred to that layer for Phase-1; this shared-secret check is
the in-app defense-in-depth minimum.

Local / container run: this module exposes a module-level ``app`` so it can be
served by gunicorn (``gunicorn service:app``) or functions-framework; running it
directly starts the Flask dev server on ``$PORT`` (Cloud Run convention).
"""

from __future__ import annotations

import hmac
import os

from flask import Flask, jsonify, request

from main import process_outbox_item as process_outbox_item_entry
from main import refresh_and_process_user
from email_automation.manual_reply import (
    bounded_manual_reply_result,
    is_canonical_document_id,
)
from email_automation.scheduler_lease import run_with_user_lease

app = Flask(__name__)

_AUTH_ENV = "PROCESS_USER_AUTH"
_MAX_UID_LENGTH = 128
_PROCESS_OUTBOX_BODY_KEYS = frozenset({"uid", "outboxId"})
_PROCESS_OUTBOX_STATUSES = frozenset({
    "manual_ready",
    "cancelled",
    "not_found",
    "blocked_state_changed",
    "blocked_non_manual",
    "blocked_generic_owned",
    "blocked_invalid_client",
    "blocked_invalid_thread",
    "blocked_invalid_notification",
    "blocked_invalid_action_audit",
    "blocked_missing_action_audit",
    "blocked_audit_status",
    "blocked_audit_actor",
    "blocked_audit_source",
    "blocked_audit_action_type",
    "blocked_audit_client",
    "blocked_audit_thread",
    "blocked_audit_notification",
    "blocked_audit_outbox",
})
_REVIEWED_PROCESS_OUTBOX_HTTP_CODES = {
    "processed": 200,
    "terminal_no_effect": 200,
    "manual_review": 409,
    "invalid": 400,
}


def _extract_bearer() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return None


def _auth_ok() -> bool:
    """Shared-secret gate. Open (True) when ``PROCESS_USER_AUTH`` is unset."""
    expected = os.getenv(_AUTH_ENV)
    if not expected:
        return True
    # TODO(auth): real OIDC ID-token verification for the Cloud Tasks invoker.
    provided = _extract_bearer() or request.headers.get("X-Process-User-Auth")
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _valid_document_id(value: str, *, max_length: int | None = None) -> bool:
    if max_length is not None and (
        not isinstance(value, str) or len(value) > max_length
    ):
        return False
    return is_canonical_document_id(value)


@app.get("/health")
@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@app.post("/process-user")
def process_user():
    if not _auth_ok():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "error": "invalid or missing JSON body"}), 400

    uid = body.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        return jsonify({"status": "error", "error": "missing uid"}), 400
    uid = uid.strip()

    try:
        acquired = run_with_user_lease(uid, lambda: refresh_and_process_user(uid))
    except Exception as e:  # noqa: BLE001 — any pipeline failure is a 500 so Tasks retries
        # Return 500 (not 200) so Cloud Tasks retries the delivery with backoff.
        return jsonify({"status": "error", "error": str(e)}), 500

    if acquired:
        return jsonify({"status": "processed", "uid": uid}), 200
    # A concurrent worker may already have taken its Firestore snapshot before
    # this request's outbox item was created. A non-2xx response keeps the Cloud
    # Task retryable instead of acknowledging work that no worker has observed.
    return jsonify({"status": "skipped_locked", "uid": uid}), 503


@app.post("/process-outbox")
def process_outbox():
    if not _auth_ok():
        return jsonify({"status": "error", "reason": "unauthorized"}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != _PROCESS_OUTBOX_BODY_KEYS:
        return jsonify({"status": "error", "reason": "invalid_request"}), 400

    uid = body["uid"]
    outbox_id = body["outboxId"]
    if not _valid_document_id(uid, max_length=_MAX_UID_LENGTH):
        return jsonify({"status": "error", "reason": "invalid_request"}), 400
    if not _valid_document_id(outbox_id):
        return jsonify({"status": "error", "reason": "invalid_request"}), 400

    outcome = {}

    def process_exact_item():
        outcome["value"] = process_outbox_item_entry(uid, outbox_id)
        return outcome["value"]

    try:
        acquired = run_with_user_lease(uid, process_exact_item)
    except Exception:  # noqa: BLE001 — preserve retry semantics without leaking internals
        return jsonify({"status": "error", "reason": "processing_failed"}), 500

    if not acquired:
        return jsonify({"status": "skipped_locked"}), 503

    result = outcome.get("value")
    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, str) and status in _PROCESS_OUTBOX_STATUSES:
        # Preserve the exact durable Task 2 transport response while the new
        # main runner no longer produces this legacy classification envelope.
        return jsonify({"status": status}), 200

    try:
        public_result = bounded_manual_reply_result(result)
    except ValueError:
        return jsonify({"status": "error", "reason": "processing_failed"}), 500
    return (
        jsonify(public_result),
        _REVIEWED_PROCESS_OUTBOX_HTTP_CODES[public_result["status"]],
    )


if __name__ == "__main__":
    # Cloud Run injects PORT; default to 8080 for local runs. Dev server only —
    # production serves this module via gunicorn (see deploy/cloudrun-service.yaml).
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
