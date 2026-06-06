import os
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import app_config

if importlib.util.find_spec("flask"):
    import app
else:
    app = None


class AppGuardrailTests(unittest.TestCase):
    def test_render_blueprint_is_removed_from_backend_repo(self):
        repo_root = Path(__file__).resolve().parents[1]
        self.assertFalse((repo_root / "render.yaml").exists())

    def test_legacy_flask_oauth_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(app_config.legacy_flask_oauth_enabled())

    @unittest.skipIf(app is None, "flask is not installed")
    def test_legacy_flask_oauth_login_redirects_to_firebase_email_access_by_default(self):
        with app.app.test_client() as client:
            response = client.get("/auth/login")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/email-access", response.headers["Location"])

    def test_cors_origin_parser_never_returns_wildcard(self):
        with patch.dict(os.environ, {"ALLOWED_CORS_ORIGINS": "http://localhost:3000,*,https://sitesift.ai"}):
            self.assertEqual(
                app_config.split_csv_env("ALLOWED_CORS_ORIGINS"),
                ["http://localhost:3000", "https://sitesift.ai"],
            )

    def test_default_cors_origins_include_current_production_domains(self):
        self.assertIn("https://sitesiftai.com", app_config.DEFAULT_CORS_ORIGINS)
        self.assertIn("https://www.sitesiftai.com", app_config.DEFAULT_CORS_ORIGINS)
        self.assertNotIn("*", app_config.DEFAULT_CORS_ORIGINS)

    def test_cors_origins_merge_safe_defaults_with_env_overrides(self):
        with patch.dict(os.environ, {"ALLOWED_CORS_ORIGINS": "*,https://custom.example"}):
            origins = app_config.cors_origins()
        self.assertIn("https://sitesiftai.com", origins)
        self.assertIn("https://custom.example", origins)
        self.assertNotIn("*", origins)

    def test_destructive_routes_are_disabled_in_production_even_when_flagged(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "ENABLE_DESTRUCTIVE_ADMIN_ROUTES": "true",
        }):
            self.assertFalse(app_config.destructive_admin_routes_enabled())

    def test_destructive_routes_require_explicit_non_production_flag(self):
        with patch.dict(os.environ, {
            "APP_ENV": "local",
            "ENABLE_DESTRUCTIVE_ADMIN_ROUTES": "true",
        }):
            self.assertTrue(app_config.destructive_admin_routes_enabled())


if __name__ == "__main__":
    unittest.main()
