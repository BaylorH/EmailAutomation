"""THE guarantee test for the signature-website change.

This code sits in the SEND PATH. The single promise the change makes is that
with both feature flags unset the outgoing footer HTML is BYTE-IDENTICAL to
production. Everything else in this feature is negotiable; this is not.

The proof is not "compare the branch to itself with the env cleared" -- that
would pass even if the branch had silently changed the footer for everyone. It
loads production's OWN ``email_automation/utils.py`` straight out of git
(``origin/main``), imports it as a separate module in the same interpreter, and
asserts that ``get_email_footer`` on that module returns exactly the same string
as ``get_email_footer`` on this branch, for every signature mode and for a
signature whose website WOULD be neutralised once the flag is on.

If this file ever goes red, the change has altered production output and must
not ship.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from email_automation import utils as branch_utils
from email_automation.signature_website_validation import (
    SIGNATURE_WEBSITE_PROBE_TIMEOUT_ENV,
    SIGNATURE_WEBSITE_REACHABILITY_ENV,
    SIGNATURE_WEBSITE_VALIDATION_ENV,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASELINE_REFS = ("origin/main", "main", "origin/HEAD")
_TARGET = "email_automation/utils.py"

_FLAG_NAMES = (
    SIGNATURE_WEBSITE_VALIDATION_ENV,
    SIGNATURE_WEBSITE_REACHABILITY_ENV,
    SIGNATURE_WEBSITE_PROBE_TIMEOUT_ENV,
)


def _flags_unset():
    """Both flags genuinely absent from the environment -- the shipping default."""
    return mock.patch.dict(
        os.environ, {name: "" for name in _FLAG_NAMES}, clear=False
    )


def _load_baseline_utils():
    """Import production's utils.py (from git) as a standalone module.

    ``utils.py`` has no package-relative imports at module scope, so it loads
    cleanly outside the package. Returns ``None`` when no baseline ref is
    resolvable (e.g. a shallow clone with no origin), so the test skips rather
    than lying.
    """
    source = None
    for ref in _BASELINE_REFS:
        try:
            source = subprocess.run(
                ["git", "show", f"{ref}:{_TARGET}"],
                cwd=_REPO_ROOT,
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        break
    if not source:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baseline_utils.py")
        with open(path, "wb") as handle:
            handle.write(source)
        spec = importlib.util.spec_from_file_location(
            "_sitesift_baseline_utils", path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module


_PROFESSIONAL_BAD = {
    "email": "agent@acme-brokerage.co",
    "signatureMode": "professional",
    "professionalSignature": {
        "name": "A Broker",
        "title": "Senior Advisor",
        "phone": "555-0100",
        "email": "agent@acme-brokerage.co",
        "company": "Acme Brokerage",
        "website": "yourcompany.com",
    },
}
_PROFESSIONAL_GOOD = {
    **_PROFESSIONAL_BAD,
    "professionalSignature": {
        **_PROFESSIONAL_BAD["professionalSignature"],
        "website": "acme-brokerage.co",
    },
}
_PROFESSIONAL_PRIVATE_IP = {
    **_PROFESSIONAL_BAD,
    "professionalSignature": {
        **_PROFESSIONAL_BAD["professionalSignature"],
        "website": "192.168.1.50",
    },
}

_CUSTOM_SIGNATURES = (
    'Jane Doe<br>Acme Brokerage<br><a href="https://yourcompany.com">yourcompany.com</a>',
    'Jane Doe<br>Acme Brokerage<br><a href="http://192.168.1.50/team">our site</a>',
    'Jane Doe<br>Acme Brokerage<br><a href="https://acme-brokerage.co">acme-brokerage.co</a>',
    "Jane Doe\nAcme Brokerage\nyourcompany.com\n555-0100",
    "",
    "   ",
)


class FooterByteIdentityAgainstProductionTests(unittest.TestCase):
    """With flags unset, this branch must emit production's exact bytes."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = _load_baseline_utils()

    def setUp(self):
        if self.baseline is None:
            self.skipTest("no baseline git ref (origin/main) resolvable here")

    def _both(self, signature, mode, user_email):
        with _flags_unset():
            branch = branch_utils.get_email_footer(
                signature, mode, user_email=user_email
            )
        production = self.baseline.get_email_footer(
            signature, mode, user_email=user_email
        )
        return production, branch

    def test_professional_footer_is_byte_identical_to_production(self):
        for label, profile in (
            ("placeholder website", _PROFESSIONAL_BAD),
            ("real website", _PROFESSIONAL_GOOD),
            ("private ip website", _PROFESSIONAL_PRIVATE_IP),
        ):
            with self.subTest(profile=label):
                signature, mode, email = branch_utils.resolve_signature_settings(profile)
                production, branch = self._both(signature, mode, email)
                self.assertTrue(production, "baseline produced no footer to compare")
                self.assertEqual(branch, production)

    def test_custom_footer_is_byte_identical_to_production(self):
        for signature in _CUSTOM_SIGNATURES:
            with self.subTest(signature=signature[:40]):
                production, branch = self._both(
                    signature, "custom", "jane@acme-brokerage.co"
                )
                self.assertEqual(branch, production)

    def test_every_signature_mode_is_byte_identical_to_production(self):
        signature, _, email = branch_utils.resolve_signature_settings(_PROFESSIONAL_BAD)
        for mode in (None, "", "none", "custom", "professional", "unknown-mode"):
            with self.subTest(mode=mode):
                production, branch = self._both(signature, mode, email)
                self.assertEqual(branch, production)

    def test_full_body_assembly_is_byte_identical_to_production(self):
        signature, mode, email = branch_utils.resolve_signature_settings(
            _PROFESSIONAL_BAD
        )
        body = "Hi, quick question about the listing."
        with _flags_unset():
            branch = branch_utils.format_email_body_with_footer(
                body, signature, mode, user_email=email
            )
        production = self.baseline.format_email_body_with_footer(
            body, signature, mode, user_email=email
        )
        self.assertEqual(branch, production)

    def test_the_comparison_can_actually_detect_a_difference(self):
        """Guard against a vacuous proof.

        If the flag-on footer were also identical, the three tests above would
        be asserting nothing. Turning the flag ON must change the very footer
        the flag-off tests compare.
        """
        signature, mode, email = branch_utils.resolve_signature_settings(
            _PROFESSIONAL_BAD
        )
        production = self.baseline.get_email_footer(signature, mode, user_email=email)
        with mock.patch.dict(
            os.environ, {SIGNATURE_WEBSITE_VALIDATION_ENV: "true"}, clear=False
        ):
            flagged = branch_utils.get_email_footer(signature, mode, user_email=email)
        self.assertNotEqual(flagged, production)
        self.assertIn('href="https://yourcompany.com"', production)
        self.assertNotIn("https://yourcompany.com", flagged)


class FlagsAreOffByDefaultTests(unittest.TestCase):
    """Nothing but an exact ``"true"`` may switch this on."""

    def test_a_pristine_environment_leaves_both_flags_off(self):
        from email_automation.signature_website_validation import (
            signature_website_reachability_enabled,
            signature_website_validation_enabled,
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(signature_website_validation_enabled())
            self.assertFalse(signature_website_reachability_enabled())

    def test_no_committed_config_file_turns_either_flag_on(self):
        """A stray ``=true`` in a committed config would ship this on by accident.

        Deliberately scans the repo's TRACKED deployment/config surfaces rather
        than ``os.environ`` -- running the suite with the flag exported on
        purpose (rollout step 2) must stay green; only a checked-in default
        being flipped is a defect.
        """
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
        )
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        paths = [p for p in tracked.stdout.decode().split("\0") if p]
        interesting = [
            p
            for p in paths
            if p.endswith((".env", ".yaml", ".yml", ".json", ".toml", ".sh", ".cfg", ".ini"))
            or os.path.basename(p).startswith((".env", "Dockerfile", "cloudbuild", "app."))
        ]
        offenders = []
        for rel in interesting:
            try:
                text = (pathlib.Path(_REPO_ROOT) / rel).read_text(errors="ignore")
            except OSError:
                continue
            for name in (
                SIGNATURE_WEBSITE_VALIDATION_ENV,
                SIGNATURE_WEBSITE_REACHABILITY_ENV,
            ):
                if re.search(
                    re.escape(name) + r"\s*[:=]\s*[\"\']?\s*true", text, re.IGNORECASE
                ):
                    offenders.append(f"{rel} sets {name}=true")
        self.assertEqual(
            [],
            offenders,
            "both signature-website flags must ship OFF: " + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
