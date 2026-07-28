import sys
import unittest
from pathlib import Path
from unittest import mock

from email_automation import observability


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeSentrySdk:
    def __init__(self, error=None):
        self.error = error
        self.init_calls = []

    def init(self, **kwargs):
        self.init_calls.append(kwargs)
        if self.error:
            raise self.error


class ObservabilityInitializationTests(unittest.TestCase):
    def test_init_sentry_uses_environment_release_and_safe_defaults(self):
        fake_sentry = FakeSentrySdk()
        env = {
            "SENTRY_DSN": "https://example@sentry.invalid/1",
            "SENTRY_ENVIRONMENT": "staging",
            "RENDER_GIT_COMMIT": "abc123",
            "SENTRY_TRACES_SAMPLE_RATE": "0.25",
        }

        with mock.patch.dict(sys.modules, {"sentry_sdk": fake_sentry}), mock.patch.dict(
            "os.environ",
            env,
            clear=True,
        ):
            initialized = observability.init_sentry()

        self.assertTrue(initialized)
        self.assertEqual(
            fake_sentry.init_calls,
            [{
                "dsn": "https://example@sentry.invalid/1",
                "environment": "staging",
                "release": "abc123",
                "traces_sample_rate": 0.25,
                "max_breadcrumbs": 100,
                "send_default_pii": False,
            }],
        )

    def test_init_sentry_skips_when_dsn_missing(self):
        fake_sentry = FakeSentrySdk()

        with mock.patch.dict(sys.modules, {"sentry_sdk": fake_sentry}), mock.patch.dict(
            "os.environ",
            {},
            clear=True,
        ):
            initialized = observability.init_sentry()

        self.assertFalse(initialized)
        self.assertEqual(fake_sentry.init_calls, [])

    def test_sdk_initialization_failure_is_optional(self):
        fake_sentry = FakeSentrySdk(RuntimeError("observer unavailable"))
        with mock.patch.dict(sys.modules, {"sentry_sdk": fake_sentry}), mock.patch.dict(
            "os.environ",
            {"SENTRY_DSN": "https://example@sentry.invalid/1"},
            clear=True,
        ):
            initialized = observability.init_sentry()

        self.assertFalse(initialized)

    def test_both_worker_entry_surfaces_initialize_observability(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        service_source = (REPO_ROOT / "service.py").read_text(encoding="utf-8")

        self.assertIn("init_sentry()", main_source)
        self.assertIn("init_sentry()", service_source)

    def test_sentry_runtime_is_declared_and_hash_locked(self):
        requirements = (
            REPO_ROOT / "requirements.txt"
        ).read_text(encoding="utf-8").splitlines()
        lock = (
            REPO_ROOT / "requirements.lock"
        ).read_text(encoding="utf-8")

        self.assertIn("sentry-sdk", requirements)
        self.assertRegex(
            lock,
            r"(?m)^sentry-sdk==[0-9][^\s]* \\\n"
            r"(?:    --hash=sha256:[0-9a-f]{64}(?: \\\n)?)+$",
        )


if __name__ == "__main__":
    unittest.main()
