import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_CONFIGS = (
    REPO_ROOT / ".github" / "workflows" / "email.yml",
    REPO_ROOT / ".github" / "workflows" / "email-dev-scoped.yml",
    REPO_ROOT / "deploy" / "cloudrun-job.yaml",
    REPO_ROOT / "deploy" / "cloudrun-service.yaml",
)
REQUIRED_EXACT_VALUES = {
    "SITESIFT_PROVIDER_EFFECTS_ENABLED": "true",
    "SITESIFT_OUTBOUND_MODE": "live",
    "SITESIFT_EFFECT_MAX_ATTEMPTS": "3",
    "SITESIFT_EFFECT_MAX_PER_RUN": "100",
    "SITESIFT_EFFECT_MAX_PER_USER": "50",
    "SITESIFT_EFFECT_MAX_PER_PROVIDER": "100",
}


def _yaml_environment_value(source: str, name: str) -> str | None:
    github_match = re.search(
        rf"^\s*{re.escape(name)}:\s*[\"']?([^\"'\s#]+)[\"']?\s*$",
        source,
        flags=re.MULTILINE,
    )
    if github_match:
        return github_match.group(1)
    cloud_run_match = re.search(
        rf"^\s*-\s*name:\s*{re.escape(name)}\s*$"
        rf"\s*^\s*value:\s*[\"']?([^\"'\s#]+)[\"']?\s*$",
        source,
        flags=re.MULTILINE,
    )
    return cloud_run_match.group(1) if cloud_run_match else None


class EffectGatewayLaunchConfigTests(unittest.TestCase):
    def test_every_worker_runtime_explicitly_authorizes_the_fail_closed_gateway(self):
        for path in WORKER_CONFIGS:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                actual = {
                    name: _yaml_environment_value(source, name)
                    for name in REQUIRED_EXACT_VALUES
                }
                self.assertEqual(actual, REQUIRED_EXACT_VALUES)

    def test_caps_are_positive_and_outbound_authority_is_exact(self):
        self.assertEqual(
            REQUIRED_EXACT_VALUES["SITESIFT_PROVIDER_EFFECTS_ENABLED"],
            "true",
        )
        self.assertEqual(
            REQUIRED_EXACT_VALUES["SITESIFT_OUTBOUND_MODE"],
            "live",
        )
        for name, value in REQUIRED_EXACT_VALUES.items():
            if "_MAX_" in name:
                self.assertGreater(int(value), 0)

    def test_runtime_run_identity_has_stable_github_and_process_paths(self):
        source = (
            REPO_ROOT / "email_automation" / "graph_final_send.py"
        ).read_text(encoding="utf-8")

        self.assertIn('os.getenv("GITHUB_RUN_ID")', source)
        self.assertIn('os.getenv("GITHUB_RUN_ATTEMPT", "1")', source)
        self.assertIn("_PROCESS_RUN_ID", source)


if __name__ == "__main__":
    unittest.main()
