"""The certification twin's deployment contract, pinned.

Every safety property of the twin that is NOT enforced by code lives in this
manifest: the identity it runs as, the fact it is unreachable from outside, the
digest-pinned image, and -- most of it -- the credentials it deliberately does
not carry.

None of that is self-checking. A secret re-added "to debug something" converts
the twin into a service that can send real mail, and nothing else in the build
would notice. So the absences are asserted here as explicitly as the presences.
"""

import os
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "deploy" / "cloudrun-certification-service.yaml"
ORDINARY_MANIFEST = REPO_ROOT / "deploy" / "cloudrun-service.yaml"

# Re-adding ANY of these makes a real external effect reachable from the twin.
FORBIDDEN_ENV = frozenset({
    "AZURE_API_CLIENT_SECRET",
    "AZURE_API_APP_ID",
    "OPENAI_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "FIREBASE_API_KEY",
    "FIREBASE_BUCKET",
    "PROCESS_USER_AUTH",
    "GOOGLE_APPLICATION_CREDENTIALS",
})


def _load(path):
    return yaml.safe_load(path.read_text())


def _container(document):
    return document["spec"]["template"]["spec"]["containers"][0]


def _env_names(document):
    return {entry["name"] for entry in _container(document).get("env", [])}


class CertificationTwinServiceContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.doc = _load(MANIFEST)

    def test_the_service_name_is_exactly_what_the_in_app_fence_keys_on(self):
        """service.py makes ordinary routes inert on this exact name. A rename
        here silently re-enables /process-user on the twin."""
        import service
        self.assertEqual(self.doc["metadata"]["name"], service.CERTIFICATION_SERVICE)
        env = {e["name"]: e.get("value") for e in _container(self.doc)["env"]}
        self.assertEqual(env.get("K_SERVICE"), service.CERTIFICATION_SERVICE)

    def test_ingress_is_internal_so_there_is_no_public_route(self):
        annotations = self.doc["metadata"]["annotations"]
        self.assertEqual(annotations["run.googleapis.com/ingress"], "internal")

    def test_the_image_is_pinned_by_digest_not_by_tag(self):
        """A tag can be repointed after review; a digest is the identity the
        stamp binds to."""
        image = _container(self.doc)["image"]
        self.assertIn("@sha256:", image)
        # The digest itself contains a colon, so check the REPOSITORY half:
        # `repo@sha256:...` is pinned, `repo:tag@sha256:...` still names a tag.
        repository = image.split("@", 1)[0]
        self.assertNotIn(":", repository.rsplit("/", 1)[-1], f"tag present: {image}")

    def test_the_twin_does_not_run_as_the_ordinary_service_account(self):
        twin = self.doc["spec"]["template"]["spec"]["serviceAccountName"]
        ordinary = _load(ORDINARY_MANIFEST)["spec"]["template"]["spec"]["serviceAccountName"]
        self.assertNotEqual(twin, ordinary)
        self.assertTrue(twin.startswith("sitesift-certification-runtime@"), twin)

    def test_no_provider_credential_reaches_the_twin(self):
        leaked = sorted(_env_names(self.doc) & FORBIDDEN_ENV)
        self.assertEqual(leaked, [], f"credential(s) reachable from the twin: {leaked}")

    def test_the_ordinary_service_really_does_carry_those_credentials(self):
        """Guards against a vacuous absence assertion.

        If the ordinary service did not carry these either, the test above would
        pass while proving nothing about the twin.
        """
        carried = _env_names(_load(ORDINARY_MANIFEST)) & FORBIDDEN_ENV
        self.assertTrue(carried, "ordinary service carries none of them; the "
                                 "absence assertion above is vacuous")

    def test_the_run_is_bound_to_an_exact_revision_and_image(self):
        env = _env_names(self.doc)
        self.assertIn("SITESIFT_SOURCE_REVISION", env)
        self.assertIn("SITESIFT_IMAGE_DIGEST", env)

    def test_the_fixture_secret_is_pinned_to_a_version_never_latest(self):
        """A stamp must bind the exact secret version it executed against."""
        for entry in _container(self.doc)["env"]:
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
            if ref:
                self.assertNotEqual(ref.get("key"), "latest",
                                    f"{entry['name']} floats on 'latest'")

    def test_exactly_one_run_may_be_in_flight(self):
        """Two concurrent runs sharing an instance could interleave fixture
        state, and a run that cannot be attributed to one fixture is not
        evidence."""
        spec = self.doc["spec"]["template"]["spec"]
        self.assertEqual(spec["containerConcurrency"], 1)
        self.assertEqual(
            self.doc["spec"]["template"]["metadata"]
            ["annotations"]["autoscaling.knative.dev/maxScale"], "1")


if __name__ == "__main__":
    unittest.main()
