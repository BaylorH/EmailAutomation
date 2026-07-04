"""WS-B: .dockerignore contract — keep secrets and cruft out of the image.

The Dockerfile does `COPY . .`, and .gitignore does NOT filter the docker
build context: a local `docker build` in a working checkout would bake
service-account.json, .env, token caches, run_production.sh (contains
credentials per the .gitignore comment), tests/, and .git into the pushed
image. Docker is not available in this environment, so this test pins the
committed .dockerignore contents instead; when Docker IS available (CI or
laptop), additionally verify with:

    docker build -t email-automation:audit .
    docker run --rm --entrypoint sh email-automation:audit -c \
        "find / -name 'service-account*' -o -name '.env*' -o -name '*token_cache*' 2>/dev/null"

which must print nothing.
"""

import os
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERIGNORE_PATH = os.path.join(REPO_ROOT, ".dockerignore")

# Every pattern here must appear verbatim as an active (non-comment) line.
REQUIRED_PATTERNS = [
    # Credentials / secrets
    "service-account*.json",
    "*credentials*.json",
    ".env*",
    "*.pem",
    "*.key",
    "run_production.sh",
    # Token caches (per-user MSAL state; never bake a user's tokens into an image)
    "msal_token_cache.bin",
    "token_cache.bin",
    # Repo/dev cruft that has no place in a runtime image
    ".git",
    "tests/",
    "test_pdfs/",
    "__pycache__",
]


class DockerignoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DOCKERIGNORE_PATH):
            raise AssertionError(
                ".dockerignore is missing at repo root — the Dockerfile does "
                "`COPY . .`, so without it a local build bakes secrets "
                "(service-account.json, .env, token caches) into the image."
            )
        with open(DOCKERIGNORE_PATH, "r") as f:
            cls.active_lines = {
                line.strip()
                for line in f.read().splitlines()
                if line.strip() and not line.strip().startswith("#")
            }

    def test_sensitive_and_cruft_patterns_present(self):
        missing = [p for p in REQUIRED_PATTERNS if p not in self.active_lines]
        self.assertEqual(
            [],
            missing,
            f".dockerignore is missing required exclusion patterns: {missing}",
        )

    def test_runtime_essentials_not_excluded(self):
        """Guard against over-excluding: the job needs these to run."""
        for essential in ("main.py", "requirements.txt", "email_automation", "email_automation/"):
            self.assertNotIn(
                essential,
                self.active_lines,
                f".dockerignore must not exclude runtime-essential path {essential!r}",
            )


if __name__ == "__main__":
    unittest.main()
