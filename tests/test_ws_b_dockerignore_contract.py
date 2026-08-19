"""WS-B: .dockerignore contract — keep secrets and cruft out of the image.

The Dockerfile does `COPY . .`, and .gitignore does NOT filter the docker
build context: a local `docker build` in a working checkout would bake
service-account.json, .env, token caches, run_production.sh (contains
credentials per the .gitignore comment), tests/, and .git into the pushed
image.

WHY THIS FILE WAS REWRITTEN
---------------------------
The previous version asserted that certain patterns appeared *verbatim* as
lines in .dockerignore. That is a presence test, and presence is not effect:
`venv/` was present for the whole life of the file while `auth_service/venv`
-- 190 MB, 2763 files -- shipped in every image anyway, because Docker anchors
.dockerignore patterns at the CONTEXT ROOT. `venv/` excludes a top-level
`venv`; it never matches `auth_service/venv`. Depth needs `**/venv/`. The
contract was green the entire time the bug was live.

So every assertion here now runs a PATH through the repo's own .dockerignore
consumer -- email_automation.certification.image_manifest, the same module
scripts/verify_image_source_manifest.py uses to recompute the deployable set --
and asserts what happens to that path. A pattern nobody can name is fine as
long as the file is out; a pattern spelled perfectly is a failure if the file
is still in.

Two guards keep these assertions from becoming tautologies:

  * test_the_depth_assertion_fails_against_a_root_anchored_pattern mutates the
    venv rule back to root-anchored `venv/` and requires the depth path to be
    INCLUDED. A pinning test that only agrees with itself proves nothing, and
    that is precisely the bug being fixed here.
  * the depth cases use a venv path that does not exist in this repo, so a
    literal `auth_service/venv/` rule cannot satisfy them -- the pattern has to
    be genuinely depth-general.

Docker is not available in this environment. When it IS (CI or laptop),
additionally verify with:

    docker build -t email-automation:audit .
    docker run --rm --entrypoint sh email-automation:audit -c \
        "find / -name 'service-account*' -o -name '.env*' -o -name '*token_cache*' 2>/dev/null"

which must print nothing, and:

    docker run --rm --entrypoint sh email-automation:audit -c \
        "find /app -type d -name venv"

which must also print nothing.
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import image_manifest as im

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"

# Paths that must NOT reach the image, each named for the reason it must not.
# Every entry is a PATH, not a pattern: the assertion is about the effect of
# the ignore file, not about its wording.
MUST_BE_EXCLUDED = {
    # --- credentials, at the root AND nested ---
    "service-account.json": "service account key",
    "auth_service/service-account.json": "service account key, nested",
    "my-credentials.json": "credentials blob",
    "email_automation/oauth-credentials.json": "credentials blob, nested",
    ".env": "environment secrets",
    "auth_service/.env.production": "environment secrets, nested",
    "server.pem": "private key material",
    "auth_service/certs/server.pem": "private key material, nested",
    "server.key": "private key material",
    "deploy/tls/server.key": "private key material, nested",
    "run_production.sh": "contains credentials per .gitignore",
    # --- per-user token caches ---
    "msal_token_cache.bin": "a user's MSAL tokens",
    "auth_service/msal_token_cache.bin": "a user's MSAL tokens, nested",
    "token_cache.bin": "a user's OAuth tokens",
    # --- committed virtualenvs: 190 MB of vendored binaries, at ANY depth ---
    "venv/bin/python": "committed virtualenv at the root",
    "auth_service/venv/bin/python": "committed virtualenv one level down",
    "auth_service/venv/lib/python3.12/site-packages/certifi/cacert.pem": (
        "committed virtualenv, deep"),
    "services/scheduler/venv/pyvenv.cfg": "committed virtualenv, any depth",
    ".venv/bin/python": "committed virtualenv, dot-prefixed",
    "auth_service/.venv/bin/python": "committed virtualenv, dot-prefixed, nested",
    # --- repo/dev cruft with no place in a runtime image ---
    ".git/config": "VCS metadata",
    "tests/test_x.py": "test suite",
    "test_pdfs/sample.pdf": "test fixtures",
    "scripts/deploy.sh": "dev scaffolding",
    "Dockerfile": "build scaffolding",
    "__pycache__/x.pyc": "interpreter cache",
    "email_automation/__pycache__/x.pyc": "interpreter cache, nested",
    "email_automation/certification/__pycache__/y.pyc": "interpreter cache, deep",
    "README.md": "documentation",
    "email_automation/NOTES.md": "documentation, nested",
    ".DS_Store": "OS cruft",
    "email_automation/.DS_Store": "OS cruft, nested",
    "app.log": "local log",
    "auth_service/logs/app.log": "local log, nested",
}

# The job cannot run without these. Guards against over-excluding.
MUST_BE_INCLUDED = (
    "main.py",
    "service.py",
    "config.py",
    "requirements.lock",
    "email_automation/email.py",
    "email_automation/certification/runner.py",
    "email_automation/certification/image_manifest.py",
)

_VENV_DEPTH_PATH = "services/scheduler/venv/pyvenv.cfg"


def _rules():
    return im.load_dockerignore(DOCKERIGNORE_PATH)


class DockerignoreEffectTests(unittest.TestCase):
    """What the ignore file DOES to a path, not what it says."""

    @classmethod
    def setUpClass(cls):
        if not DOCKERIGNORE_PATH.exists():
            raise AssertionError(
                ".dockerignore is missing at repo root — the Dockerfile does "
                "`COPY . .`, so without it a local build bakes secrets "
                "(service-account.json, .env, token caches) into the image."
            )
        cls.rules = _rules()

    def test_nothing_that_must_stay_out_is_deployable(self):
        leaked = [
            f"{path} ({why})"
            for path, why in sorted(MUST_BE_EXCLUDED.items())
            if not im.is_excluded(path, self.rules)
        ]
        self.assertEqual(
            [],
            leaked,
            "these paths are still in the docker build context:\n  "
            + "\n  ".join(leaked),
        )

    def test_a_committed_virtualenv_is_excluded_at_any_depth(self):
        """The regression this file exists for.

        `venv/` is root-anchored: Docker matches .dockerignore patterns against
        the path relative to the CONTEXT ROOT, so it excludes a top-level
        `venv` and nothing else. auth_service/venv shipped anyway.
        """
        for path in (
            "venv/bin/python",
            "auth_service/venv/bin/activate",
            "auth_service/venv/lib/python3.12/site-packages/pip/__init__.py",
            _VENV_DEPTH_PATH,
            "a/b/c/d/venv/bin/python",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    im.is_excluded(path, self.rules),
                    f"{path} would be COPYed into the image",
                )

    def test_runtime_essentials_are_still_deployable(self):
        for path in MUST_BE_INCLUDED:
            with self.subTest(path=path):
                self.assertFalse(
                    im.is_excluded(path, self.rules),
                    f".dockerignore excludes runtime-essential {path!r}",
                )

    def test_no_virtualenv_survives_in_the_real_checkout_manifest(self):
        """End to end: the deployable set recomputed from THIS checkout.

        The unit assertions above run synthetic paths through the matcher.
        This one runs the real tree, which is where the 2763 files actually
        were.
        """
        manifest = im.manifest_from_checkout(REPO_ROOT)
        offenders = [
            entry["path"]
            for entry in manifest["files"]
            if "venv" in entry["path"].split("/")[:-1]
            or ".venv" in entry["path"].split("/")[:-1]
        ]
        self.assertEqual(
            [],
            offenders[:20],
            f"{len(offenders)} virtualenv files are in the deployable set",
        )


class DepthAssertionBitesTests(unittest.TestCase):
    """Prove the depth assertions are sensitive to the pattern.

    Without this, `test_a_committed_virtualenv_is_excluded_at_any_depth` could
    be passing for some unrelated reason and nobody would know -- which is
    exactly how the previous contract stayed green while the venv shipped.
    """

    def _rules_with_venv_rule(self, replacement):
        rules = []
        for rule in _rules():
            pattern = rule.pattern
            if pattern.rstrip("/").endswith("venv") and not pattern.startswith("."):
                pattern = replacement
            rules.append(im.Rule(pattern=pattern, negated=rule.negated))
        return rules

    def test_the_depth_assertion_fails_against_a_root_anchored_pattern(self):
        """Revert to `venv/` and the depth path must come back."""
        mutated = self._rules_with_venv_rule("venv/")
        self.assertFalse(
            im.is_excluded(_VENV_DEPTH_PATH, mutated),
            "the root-anchored pattern `venv/` appears to exclude a nested "
            "venv — the depth assertion is not measuring what it claims to",
        )
        self.assertTrue(
            im.is_excluded("venv/pyvenv.cfg", mutated),
            "`venv/` must still exclude a TOP-LEVEL venv",
        )

    def test_removing_the_venv_rule_entirely_breaks_the_contract(self):
        rules = [r for r in _rules() if not r.pattern.rstrip("/").endswith("venv")]
        self.assertFalse(im.is_excluded(_VENV_DEPTH_PATH, rules))
        self.assertFalse(im.is_excluded("venv/pyvenv.cfg", rules))


class DockerignoreStaysInSyncTests(unittest.TestCase):
    """The file's own header promises it is pinned here. Keep that true."""

    def test_every_active_line_is_reachable_as_a_rule(self):
        text = DOCKERIGNORE_PATH.read_text()
        active = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(len(active), len(_rules()))
        self.assertTrue(active, ".dockerignore has no active patterns at all")

    def test_depth_intent_is_written_down_not_implied(self):
        """A pattern meant to apply at depth must SAY so with `**/`.

        Not a presence test in disguise: this asserts the *class* of pattern,
        because a root-anchored pattern that happens to match today's layout
        silently stops working the moment a directory moves.
        """
        active = {
            line.strip()
            for line in DOCKERIGNORE_PATH.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        root_anchored_secrets = [
            p for p in active
            if not p.startswith("**/")
            and any(token in p for token in ("credential", "service-account",
                                             ".env", ".pem", ".key",
                                             "token_cache", "venv"))
        ]
        self.assertEqual(
            [],
            sorted(root_anchored_secrets),
            "these secret/virtualenv patterns are anchored at the context root "
            "and therefore miss every nested copy",
        )


if __name__ == "__main__":
    unittest.main()
