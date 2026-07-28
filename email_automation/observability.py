"""Optional worker observability initialization."""

from __future__ import annotations

import os


def _traces_sample_rate() -> float:
    try:
        rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))
    except (TypeError, ValueError):
        return 0.0
    return rate if 0.0 <= rate <= 1.0 else 0.0


def init_sentry() -> bool:
    """Initialize Sentry when configured, without becoming a worker dependency."""

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        print("⚠️ SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=(
                os.getenv("SENTRY_ENVIRONMENT")
                or os.getenv("ENVIRONMENT")
                or "production"
            ),
            release=(
                os.getenv("SENTRY_RELEASE")
                or os.getenv("RELEASE")
                or os.getenv("RENDER_GIT_COMMIT")
                or "unknown"
            ),
            traces_sample_rate=_traces_sample_rate(),
            max_breadcrumbs=100,
            send_default_pii=False,
        )
    except Exception:
        print("⚠️ Sentry initialization failed; worker will continue")
        return False
    return True
