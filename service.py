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
import re

from flask import Flask, jsonify, request

from main import process_outbox_item as process_outbox_item_entry
from main import refresh_and_process_user
from email_automation.scheduler_lease import run_with_user_lease

app = Flask(__name__)

_AUTH_ENV = "PROCESS_USER_AUTH"
_MAX_UID_LENGTH = 128
_MAX_FIRESTORE_DOCUMENT_ID_BYTES = 1500
_PROCESS_OUTBOX_BODY_KEYS = frozenset({"uid", "outboxId"})
_PROCESS_OUTBOX_STATUSES = frozenset({
    "manual_ready",
    "cancelled",
    "not_found",
    "blocked_state_changed",
    "blocked_non_manual",
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
    if value != value.strip():
        return False
    if value in {".", ".."} or "/" in value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if max_length is not None and len(value) > max_length:
        return False
    return len(value.encode("utf-8")) <= _MAX_FIRESTORE_DOCUMENT_ID_BYTES



# ---------------------------------------------------------------------------
# Bidirectional service fence
# ---------------------------------------------------------------------------
#
# The certification twin runs the EXACT immutable candidate image. That is the
# whole point -- certifying a differently-built artifact would prove nothing
# about the one being shipped. The consequence is that the twin's image also
# contains `/process-user` and `/process-outbox`, and the certification routes
# ship inside ordinary production.
#
# So the fence runs in BOTH directions, and it runs in `before_request`: ahead
# of body parsing, ahead of any provider client, ahead of the lease. A route
# that validates before it refuses has already touched what it meant to refuse,
# and a route whose refusal depends on the body leaks which service answered.

ORDINARY_SERVICE = "process-user"
CERTIFICATION_SERVICE = "process-user-certification"

# Cloud Run always injects K_SERVICE. Absent means "not on Cloud Run" -- a local
# dev server or a test process -- which stays ordinary so existing behavior is
# unchanged. A PRESENT but unrecognised name is different: the deployment
# changed in a way this fence cannot reason about, and guessing either way is
# worse than refusing both families.
_SHARED_PATHS = frozenset({"/health", "/healthz"})
_CERTIFICATION_PREFIX = "/certification/"


def _service_identity() -> str:
    return (os.getenv("K_SERVICE") or ORDINARY_SERVICE).strip()


def _route_family_allowed(path: str, identity: str) -> bool:
    if path in _SHARED_PATHS:
        return True
    if path.startswith(_CERTIFICATION_PREFIX):
        return identity == CERTIFICATION_SERVICE
    return identity == ORDINARY_SERVICE


@app.before_request
def _enforce_service_fence():
    """Refuse the other service's routes before anything else happens."""
    if _route_family_allowed(request.path, _service_identity()):
        return None
    # 404, not 403: a distinguishable rejection is a probe that tells a caller
    # which service it reached. The body is constant for the same reason.
    return jsonify({"status": "error", "reason": "route_not_available"}), 404


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
    if not isinstance(status, str) or status not in _PROCESS_OUTBOX_STATUSES:
        return jsonify({"status": "error", "reason": "processing_failed"}), 500
    return jsonify({"status": status}), 200



# ---------------------------------------------------------------------------
# Private revision-bound certification routes
# ---------------------------------------------------------------------------
#
# Reachable ONLY on `process-user-certification` (see the fence above). The
# request surface is closed: a caller names an approved scenario, a run id, and
# the revision it expects. It may never name a user, client, recipient, body,
# spreadsheet, thread, resource location, or oracle -- if it could, certification
# could be pointed at a real person or made to assert its own success, and no
# stamp would mean anything.
#
# NOT YET IMPLEMENTED, deliberately and visibly: the prepare/claim lifecycle
# needs the permanent certification run ledger, which is a separate task. These
# routes validate the locked schema and then return 501. A 501 here is a real
# answer -- the route exists, the fence admits it, the schema is enforced -- and
# it is never mistaken for a verdict, because a verdict can only come from the
# runner's terminal record.


# --- revision binding ------------------------------------------------------
#
# A stamp binds a verdict to an exact source revision and image digest. If a
# route answered a request naming some other revision, the stamp would certify
# code that never executed -- worse than no stamp, because it reads as proof.
#
# Both values are injected by the deployment manifest and must be present,
# canonical, and equal on candidate and twin. Absent or malformed is 503
# (the instrument is unavailable), NOT 400 (the caller is wrong): a caller has
# to be able to tell "my request was bad" from "this service cannot certify
# anything right now", because only one of those is worth retrying.

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _revision_binding():
    """(source_revision, image_digest) or None when either is unusable."""
    revision = (os.getenv("SITESIFT_SOURCE_REVISION") or "").strip()
    image = (os.getenv("SITESIFT_IMAGE_DIGEST") or "").strip()
    if not _FULL_SHA.match(revision) or not _IMAGE_DIGEST.match(image):
        return None
    return revision, image


_CERTIFICATION_RUN_KEYS = frozenset({"scenarioId", "runId", "expectedRevision"})
_CERTIFICATION_RUN_SCOPED_KEYS = frozenset({"runId", "expectedRevision"})

_CERTIFICATION_OPERATIONS = {
    "prepare": _CERTIFICATION_RUN_KEYS,
    "run": _CERTIFICATION_RUN_KEYS,
    "status": _CERTIFICATION_RUN_SCOPED_KEYS,
    "review-input": _CERTIFICATION_RUN_SCOPED_KEYS,
    "abort": _CERTIFICATION_RUN_SCOPED_KEYS,
    "recover": _CERTIFICATION_RUN_SCOPED_KEYS,
    "cleanup": _CERTIFICATION_RUN_SCOPED_KEYS,
}

_CERTIFICATION_REVIEW_KEYS = frozenset(
    {"runId", "expectedRevision", "reviewSetDigest", "rubricVersion", "reviews"}
)


def _certification_schema_error(body, allowed_keys) -> str | None:
    """Exact keys, exact types. Anything else is a refusal, not a coercion."""
    if not isinstance(body, dict):
        return "invalid_request"
    if set(body) != set(allowed_keys):
        return "invalid_request"
    for key in allowed_keys:
        value = body[key]
        if not isinstance(value, str) or not value or value != value.strip():
            return "invalid_request"
    return None


@app.post("/certification/<operation>")
def certification_operation(operation: str):
    binding = _revision_binding()
    if binding is None:
        # Checked BEFORE the body: an unavailable binding is not a bad request,
        # and reporting it as one would send a caller to fix the wrong thing.
        return jsonify({"status": "error",
                        "reason": "revision_binding_unavailable"}), 503
    source_revision, _image_digest = binding

    if operation == "review":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != _CERTIFICATION_REVIEW_KEYS:
            return jsonify({"status": "error", "reason": "invalid_request"}), 400
        if not isinstance(body.get("reviews"), list):
            return jsonify({"status": "error", "reason": "invalid_request"}), 400
        return jsonify({"status": "error", "reason": "not_implemented"}), 501

    allowed = _CERTIFICATION_OPERATIONS.get(operation)
    if allowed is None:
        return jsonify({"status": "error", "reason": "route_not_available"}), 404

    body = request.get_json(silent=True)
    reason = _certification_schema_error(body, allowed)
    if reason:
        return jsonify({"status": "error", "reason": reason}), 400

    if not hmac.compare_digest(body["expectedRevision"], source_revision):
        return jsonify({"status": "error", "reason": "revision_mismatch"}), 409

    return jsonify({"status": "error", "reason": "not_implemented"}), 501


if __name__ == "__main__":
    # Cloud Run injects PORT; default to 8080 for local runs. Dev server only —
    # production serves this module via gunicorn (see deploy/cloudrun-service.yaml).
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
