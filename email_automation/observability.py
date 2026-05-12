import os


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        print("⚠️ SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", os.getenv("ENVIRONMENT", "production")),
        release=(
            os.getenv("SENTRY_RELEASE")
            or os.getenv("RELEASE")
            or os.getenv("RENDER_GIT_COMMIT")
            or "unknown"
        ),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
        max_breadcrumbs=100,
        send_default_pii=False,
    )
    return True
