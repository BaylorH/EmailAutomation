import sys
import types
import unittest
from unittest.mock import patch

from email_automation import observability


class FakeSentrySdk:
    def __init__(self):
        self.init_calls = []

    def init(self, **kwargs):
        self.init_calls.append(kwargs)


class ObservabilityInitializationTests(unittest.TestCase):
    def test_init_sentry_uses_environment_and_release_context(self):
        fake_sentry = FakeSentrySdk()
        env = {
            "SENTRY_DSN": "https://example@sentry.invalid/1",
            "SENTRY_ENVIRONMENT": "staging",
            "RENDER_GIT_COMMIT": "abc123",
        }

        with patch.dict(sys.modules, {"sentry_sdk": fake_sentry}):
            with patch.dict("os.environ", env, clear=True):
                initialized = observability.init_sentry()

        self.assertTrue(initialized)
        self.assertEqual(fake_sentry.init_calls, [{
            "dsn": "https://example@sentry.invalid/1",
            "environment": "staging",
            "release": "abc123",
            "traces_sample_rate": 0.0,
            "max_breadcrumbs": 100,
            "send_default_pii": False,
        }])

    def test_init_sentry_skips_when_dsn_missing(self):
        fake_sentry = FakeSentrySdk()

        with patch.dict(sys.modules, {"sentry_sdk": fake_sentry}):
            with patch.dict("os.environ", {}, clear=True):
                initialized = observability.init_sentry()

        self.assertFalse(initialized)
        self.assertEqual(fake_sentry.init_calls, [])


if __name__ == "__main__":
    unittest.main()
