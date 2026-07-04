# Cloud Run Job image for the SiteSiftAI EmailAutomation scheduler worker.
# Entry point is `python main.py` (the live per-user pipeline wrapped in the
# Firestore single-runner lease). Auth is via ADC — Cloud Run injects the
# job's service account, so firestore.Client() needs no key file.
#
# The GitHub Actions workflow pins python-version '3.x'; 3.12-slim is chosen
# here because every requirement (PyMuPDF, Pillow, lxml, pdfplumber, ...) ships
# a manylinux wheel for 3.12, keeping the build wheel-only and layers minimal.
FROM python:3.12-slim

# - PYTHONUNBUFFERED: stream logs to Cloud Logging without buffering
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image
# - PIP_NO_CACHE_DIR: smaller image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source.
COPY . .

# Run as an unprivileged user. /app (== WORKDIR == CWD) is chowned to appuser
# so the token cache (msal_token_cache.bin), which the pipeline writes next to
# itself via a relative path, remains writable at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Cloud Run Jobs invoke the container's entrypoint once per task; the lease
# guarantees only one task does real work even if tasks/retries overlap.
ENTRYPOINT ["python", "main.py"]
