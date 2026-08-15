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
    Classifies only that exact outbox document under the same per-user lease.
    This transport-only route does not permit a send; manual items are handed
    off as ``manual_ready`` for a later reviewed sender task.
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
from email_automation.scheduler_lease import run_with_user_lease

app = Flask(__name__)

_AUTH_ENV = "PROCESS_USER_AUTH"
_MAX_UID_LENGTH = 128
_MAX_FIRESTORE_DOCUMENT_ID_BYTES = 1500


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
    if not isinstance(value, str) or not value:
        return False
    if value in {".", ".."} or "/" in value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if max_length is not None and len(value) > max_length:
        return False
    return len(value.encode("utf-8")) <= _MAX_FIRESTORE_DOCUMENT_ID_BYTES


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
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "error": "invalid or missing JSON body"}), 400

    raw_uid = body.get("uid")
    raw_outbox_id = body.get("outboxId")
    if not isinstance(raw_uid, str) or not raw_uid.strip():
        return jsonify({"status": "error", "error": "missing uid"}), 400
    if not isinstance(raw_outbox_id, str) or not raw_outbox_id.strip():
        return jsonify({"status": "error", "error": "missing outboxId"}), 400

    uid = raw_uid.strip()
    outbox_id = raw_outbox_id.strip()
    if not _valid_document_id(uid, max_length=_MAX_UID_LENGTH):
        return jsonify({"status": "error", "error": "invalid uid"}), 400
    if not _valid_document_id(outbox_id):
        return jsonify({"status": "error", "error": "invalid outboxId"}), 400

    outcome = {}

    def process_exact_item():
        outcome["value"] = process_outbox_item_entry(uid, outbox_id)
        return outcome["value"]

    try:
        acquired = run_with_user_lease(uid, process_exact_item)
    except Exception as e:  # noqa: BLE001 — preserve retry semantics for the task delivery
        return jsonify({"status": "error", "error": str(e)}), 500

    if not acquired:
        return jsonify({
            "status": "skipped_locked",
            "uid": uid,
            "outboxId": outbox_id,
        }), 503

    result = outcome.get("value")
    if not isinstance(result, dict):
        result = {"status": "processed", "uid": uid, "outboxId": outbox_id}
    return jsonify(result), 200


if __name__ == "__main__":
    # Cloud Run injects PORT; default to 8080 for local runs. Dev server only —
    # production serves this module via gunicorn (see deploy/cloudrun-service.yaml).
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
