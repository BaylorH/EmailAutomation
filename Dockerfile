# Cloud Run Job image for the SiteSiftAI EmailAutomation scheduler worker.
# Entry point is `python main.py` (the live per-user pipeline wrapped in the
# Firestore single-runner lease). Auth is via ADC — Cloud Run injects the
# job's service account, so firestore.Client() needs no key file.
#
# The GitHub Actions workflow pins python-version '3.x'; 3.12-slim is chosen
# here because every requirement (PyMuPDF, Pillow, lxml, pdfplumber, ...) ships
# a manylinux wheel for 3.12, keeping the build wheel-only and layers minimal.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

# - PYTHONUNBUFFERED: stream logs to Cloud Logging without buffering
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image
# - PIP_NO_CACHE_DIR: smaller image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source-only changes. The lock
# is resolved for Python 3.12/Linux and includes hashes for every distribution.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Application source.
COPY . .

# Record the deployable source bytes actually present in this layer.
#
# A test suite run against a checkout proves something about the CHECKOUT. A
# certification stamp is about the IMAGE, and those are the same bytes only if
# somebody checks. `COPY . .` behind a .dockerignore is precisely what drifts
# quietly -- widen one ignore rule and a file the reviewer read stops shipping,
# with nothing red anywhere.
#
# Written AFTER the copy, or it would describe an empty image. Excludes itself
# (its own digest cannot be known before it is written) and interpreter caches.
# scripts/verify_image_source_manifest.py recomputes this from the reviewed
# checkout and fails on any added, omitted, or changed file.
RUN python - <<'PYMANIFEST'
import hashlib, json, os
root = "/app"
name = ".sitesift-source-manifest.json"
files = []
for base, dirs, names in os.walk(root):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for filename in names:
        if filename == name or filename.endswith((".pyc", ".pyo")):
            continue
        full = os.path.join(base, filename)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        with open(full, "rb") as handle:
            raw = handle.read()
        files.append({
            "path": os.path.relpath(full, root),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
files.sort(key=lambda entry: entry["path"])
document = {"schemaVersion": "sitesift-image-source-manifest-v1", "files": files}
with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
    json.dump(document, handle, ensure_ascii=False, sort_keys=True,
              separators=(",", ":"))
PYMANIFEST

# Run as an unprivileged user. /app (== WORKDIR == CWD) is chowned to appuser
# so the token cache (msal_token_cache.bin), which the pipeline writes next to
# itself via a relative path, remains writable at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Cloud Run Jobs invoke the container's entrypoint once per task; the lease
# guarantees only one task does real work even if tasks/retries overlap.
ENTRYPOINT ["python", "main.py"]
