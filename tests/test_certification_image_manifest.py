"""Bind certification results to the exact source bytes inside the image.

A green suite run against a checkout proves something about the checkout. The
stamp is about the IMAGE. Those are the same bytes only if someone checks, and
"COPY . ." plus a .dockerignore is exactly the kind of thing that silently
drifts -- one ignore rule added, and a file the reviewer read is no longer in
the artifact that ships.

So the image writes a manifest of its own deployable source at build time, and
the verifier recomputes the same set from the reviewed checkout under the same
.dockerignore semantics. Added, omitted, or changed bytes all fail.
"""

import hashlib
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import image_manifest as im

REPO_ROOT = Path(__file__).resolve().parents[1]


class ManifestShapeTests(unittest.TestCase):

    def setUp(self):
        self.entries = [
            {"path": "b.py", "size": 2, "sha256": "b" * 64},
            {"path": "a.py", "size": 1, "sha256": "a" * 64},
        ]

    def test_paths_are_sorted_so_the_digest_is_order_independent(self):
        first = im.build_manifest(self.entries)
        second = im.build_manifest(list(reversed(self.entries)))
        self.assertEqual([e["path"] for e in first["files"]], ["a.py", "b.py"])
        self.assertEqual(im.manifest_digest(first), im.manifest_digest(second))

    def test_unsorted_input_is_normalised_not_accepted_as_given(self):
        manifest = im.build_manifest(self.entries)
        self.assertEqual(manifest["files"], sorted(manifest["files"],
                                                   key=lambda e: e["path"]))

    def test_the_manifest_never_includes_itself(self):
        """A self-including manifest cannot be computed, only fabricated."""
        entries = self.entries + [{"path": im.MANIFEST_NAME, "size": 9, "sha256": "c" * 64}]
        manifest = im.build_manifest(entries)
        self.assertNotIn(im.MANIFEST_NAME, [e["path"] for e in manifest["files"]])

    def test_interpreter_caches_are_excluded(self):
        entries = self.entries + [
            {"path": "__pycache__/a.cpython-312.pyc", "size": 1, "sha256": "d" * 64},
            {"path": "pkg/__pycache__/b.pyc", "size": 1, "sha256": "e" * 64},
        ]
        paths = [e["path"] for e in im.build_manifest(entries)["files"]]
        self.assertEqual(paths, ["a.py", "b.py"])

    def test_a_duplicate_path_is_refused(self):
        with self.assertRaises(im.ManifestError):
            im.build_manifest(self.entries + [{"path": "a.py", "size": 1,
                                               "sha256": "f" * 64}])

    def test_a_malformed_entry_is_refused(self):
        for bad in ({"path": "a.py", "size": 1},
                    {"path": "a.py", "size": "1", "sha256": "a" * 64},
                    {"path": "", "size": 1, "sha256": "a" * 64},
                    {"path": "a.py", "size": 1, "sha256": "NOTAHEXDIGEST"},
                    {"path": "/absolute.py", "size": 1, "sha256": "a" * 64}):
            with self.subTest(entry=bad), self.assertRaises(im.ManifestError):
                im.build_manifest([bad])


class ManifestComparisonTests(unittest.TestCase):

    def _manifest(self, entries):
        return im.build_manifest(entries)

    def setUp(self):
        self.baseline = self._manifest([
            {"path": "a.py", "size": 1, "sha256": "a" * 64},
            {"path": "b.py", "size": 2, "sha256": "b" * 64},
        ])

    def test_identical_manifests_compare_clean(self):
        self.assertEqual(im.compare(self.baseline, self.baseline), [])

    def test_an_added_image_file_is_reported(self):
        other = self._manifest(list(self.baseline["files"]) +
                               [{"path": "sneaky.py", "size": 3, "sha256": "c" * 64}])
        differences = im.compare(self.baseline, other)
        self.assertTrue(any("sneaky.py" in d and "only in image" in d
                            for d in differences), differences)

    def test_an_omitted_file_is_reported(self):
        other = self._manifest([e for e in self.baseline["files"] if e["path"] != "b.py"])
        differences = im.compare(self.baseline, other)
        self.assertTrue(any("b.py" in d and "missing from image" in d
                            for d in differences), differences)

    def test_changed_bytes_are_reported_even_at_the_same_size(self):
        """Size is a hint. The digest is the check.

        A same-length edit is the one an eyeball diff of a file listing misses
        entirely.
        """
        changed = [dict(e) for e in self.baseline["files"]]
        changed[0]["sha256"] = "9" * 64
        differences = im.compare(self.baseline, self._manifest(changed))
        self.assertTrue(any("a.py" in d and "differs" in d for d in differences),
                        differences)

    def test_the_manifest_digest_moves_when_any_byte_moves(self):
        changed = [dict(e) for e in self.baseline["files"]]
        changed[1]["sha256"] = "9" * 64
        self.assertNotEqual(im.manifest_digest(self.baseline),
                            im.manifest_digest(self._manifest(changed)))


class DockerignoreSemanticsTests(unittest.TestCase):
    """The checkout side must exclude exactly what the build context excludes."""

    def test_the_real_dockerignore_rules_are_applied(self):
        rules = im.load_dockerignore(REPO_ROOT / ".dockerignore")
        for excluded in ("tests/test_x.py", "scripts/deploy.sh", "docs/a.md",
                         "README.md", "Dockerfile", ".git/config",
                         "service-account.json", "deploy/x.yaml",
                         "__pycache__/x.pyc", "a.pyc"):
            with self.subTest(path=excluded):
                self.assertTrue(im.is_excluded(excluded, rules), excluded)
        for included in ("main.py", "service.py", "requirements.lock",
                         "email_automation/email.py",
                         "email_automation/certification/runner.py"):
            with self.subTest(path=included):
                self.assertFalse(im.is_excluded(included, rules), included)

    def test_a_dockerignore_rule_change_changes_the_deployable_set(self):
        """Pins the drift this whole gate exists to catch."""
        rules = im.load_dockerignore(REPO_ROOT / ".dockerignore")
        widened = rules + [im.Rule(pattern="email_automation/", negated=False)]
        self.assertFalse(im.is_excluded("email_automation/email.py", rules))
        self.assertTrue(im.is_excluded("email_automation/email.py", widened))


class CheckoutManifestTests(unittest.TestCase):
    """The verifier's own side, computed from this repository."""

    def test_the_checkout_manifest_contains_the_real_deployable_source(self):
        manifest = im.manifest_from_checkout(REPO_ROOT)
        paths = {e["path"] for e in manifest["files"]}
        for expected in ("main.py", "service.py",
                         "email_automation/certification/runner.py"):
            self.assertIn(expected, paths)
        for excluded in ("Dockerfile", ".dockerignore"):
            self.assertNotIn(excluded, paths)
        self.assertFalse(any(p.startswith("tests/") for p in paths))
        self.assertFalse(any(p.startswith("scripts/") for p in paths))

    def test_every_recorded_digest_is_the_real_file_digest(self):
        manifest = im.manifest_from_checkout(REPO_ROOT)
        sample = next(e for e in manifest["files"] if e["path"] == "service.py")
        actual = hashlib.sha256((REPO_ROOT / "service.py").read_bytes()).hexdigest()
        self.assertEqual(sample["sha256"], actual)
        self.assertEqual(sample["size"], (REPO_ROOT / "service.py").stat().st_size)

    def test_a_missing_manifest_is_a_failure_not_an_empty_pass(self):
        with self.assertRaises(im.ManifestError):
            im.load_manifest(REPO_ROOT / "no-such-manifest.json")


class DockerfileContractTests(unittest.TestCase):
    """The image must actually write the manifest, after COPY."""

    def setUp(self):
        self.dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    def test_the_dockerfile_writes_the_manifest(self):
        self.assertIn(im.MANIFEST_NAME, self.dockerfile)

    def test_the_manifest_is_written_after_the_source_copy(self):
        """Written before COPY it would describe an empty image."""
        copy_at = self.dockerfile.index("COPY . .")
        manifest_at = self.dockerfile.index(im.MANIFEST_NAME)
        self.assertGreater(manifest_at, copy_at)


if __name__ == "__main__":
    unittest.main()
