import os
import importlib.util
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

if importlib.util.find_spec("flask"):
    import app
else:
    app = None


@unittest.skipUnless(app, "Flask is not installed in this test runtime")
class AppGuardrailTests(unittest.TestCase):
    def test_cors_origin_parser_never_returns_wildcard(self):
        with patch.dict(os.environ, {"ALLOWED_CORS_ORIGINS": "http://localhost:3000,*,https://sitesift.ai"}):
            self.assertEqual(
                app._split_csv_env("ALLOWED_CORS_ORIGINS"),
                ["http://localhost:3000", "https://sitesift.ai"],
            )

    def test_destructive_routes_are_disabled_in_production_even_when_flagged(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "ENABLE_DESTRUCTIVE_ADMIN_ROUTES": "true",
        }):
            self.assertFalse(app._destructive_admin_routes_enabled())

    def test_destructive_routes_require_explicit_non_production_flag(self):
        with patch.dict(os.environ, {
            "APP_ENV": "local",
            "ENABLE_DESTRUCTIVE_ADMIN_ROUTES": "true",
        }):
            self.assertTrue(app._destructive_admin_routes_enabled())


if __name__ == "__main__":
    unittest.main()
