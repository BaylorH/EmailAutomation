"""The certification twin's deployment contract, pinned.

Every safety property of the twin that is NOT enforced by code lives in this
manifest: the identity it runs as, the fact it is unreachable from outside, the
digest-pinned image, and -- most of it -- the credentials it deliberately does
not carry.

None of that is self-checking. A secret re-added "to debug something" converts
the twin into a service that can send real mail, and nothing else in the build
would notice. So the absences are asserted here as explicitly as the presences.

The comparator half of the same contract is pinned at the bottom of this file.
The manifest says what the twin IS; ``twin_contract`` is what refuses a
candidate/twin pair that does not match it. One of its rules -- that the
CANDIDATE may not already be carrying production traffic -- had no check at all
until now: it was caught only incidentally, by the generic post-normalization
residual comparison, and only when the two sides happened to disagree.
"""

import os
import unittest
from pathlib import Path

import yaml

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import twin_contract as tc

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

    def test_the_expected_operator_identity_is_configured(self):
        """An unconfigured verifier refuses everyone, which is safe but useless.
        The twin must actually name the operator it will accept."""
        env = _env_names(self.doc)
        for key in ("SITESIFT_CERTIFICATION_AUDIENCE",
                    "SITESIFT_CERTIFICATION_OPERATOR_EMAIL",
                    "SITESIFT_CERTIFICATION_OPERATOR_SUB"):
            self.assertIn(key, env)

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


# -- the comparator half -----------------------------------------------------
#
# The EXACT sentences the rules emit. Keying on these is the whole point: the
# generic residual comparison produces a DIFFERENT sentence for the same input,
# so a pin that accepted either -- or that matched the substring "traffic" --
# would be pinning nothing. That is precisely how two adjacent rules in this
# same comparator went decorative.
CANDIDATE_TRAFFIC_REFUSAL = (
    "candidate carries production traffic; the promotion this proof gates has "
    "already happened"
)
UNREADABLE_CANDIDATE_SHARE_REFUSAL = (
    "candidate trafficPercent is missing or unreadable; an unreadable share is "
    "not a share of zero"
)
TWIN_TRAFFIC_REFUSAL = "twin carries production traffic; it may never be a target"
RESIDUAL_MARKER = "unpaired difference after normalization"


def _candidate(**overrides):
    spec = {
        "image": "region-docker.pkg.dev/p/r/email-automation@sha256:" + "a" * 64,
        "serviceName": "process-user",
        "serviceAccount": "123-compute@developer.gserviceaccount.com",
        "trafficPercent": 0,
        "env": {
            "SITESIFT_SOURCE_REVISION": "1" * 40,
            "SITESIFT_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "OPENAI_API_KEY": "secret://openai-api-key/latest",
            "USAGE_MONTHLY_BUDGET_USD": "50",
            "AZURE_API_CLIENT_SECRET": "secret://azure/latest",
            "FIREBASE_API_KEY": "secret://firebase/latest",
            "PROCESS_USER_AUTH": "secret://auth/latest",
        },
        "containerConcurrency": 1,
        "timeoutSeconds": 540,
    }
    spec.update(overrides)
    return spec


def _twin(**overrides):
    spec = {
        "image": "region-docker.pkg.dev/p/r/email-automation@sha256:" + "a" * 64,
        "serviceName": "process-user-certification",
        "serviceAccount": "sitesift-certification-runtime@p.iam.gserviceaccount.com",
        "trafficPercent": 0,
        "env": {
            "SITESIFT_SOURCE_REVISION": "1" * 40,
            "SITESIFT_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "OPENAI_API_KEY": "secret://openai-api-key/latest",
            "USAGE_MONTHLY_BUDGET_USD": "50",
            "K_SERVICE": "process-user-certification",
            "FIRESTORE_DATABASE": "sitesift-certification",
            "CERTIFICATION_FIXTURE_CONFIG": "sitesift-certification-fixture-config:7",
        },
        "containerConcurrency": 1,
        "timeoutSeconds": 540,
    }
    spec.update(overrides)
    return spec


class CandidateTrafficIsSpecifiedNotIncidentalTests(unittest.TestCase):
    """A candidate already serving production is past the point this proves.

    The rollout holds 100% of positive traffic on the OLD revision until the
    stamp exists (``validate_topology(expected_positive=OLD_REVISION)`` in
    ``scripts/phase1_rollout.py``). Proof precedes promotion; a candidate that
    already carries a share has had the effect the proof is supposed to gate,
    and certifying it afterwards is a record, not a gate.

    That property was pinned on the rollout side and UNSPECIFIED on the
    comparator side: a traffic-carrying candidate was caught only when the
    residual comparison happened to see the two shares disagree.
    """

    def test_a_baseline_candidate_and_twin_still_compare_clean(self):
        """Vacuity guard. If the fixtures did not agree to begin with, every
        assertion below would pass for the wrong reason."""
        self.assertEqual(tc.compare(_candidate(), _twin()), [])

    def test_the_named_rule_reports_a_traffic_carrying_candidate(self):
        """Not 'some difference mentions traffic' -- THIS rule, by its own
        text."""
        self.assertIn(CANDIDATE_TRAFFIC_REFUSAL,
                      tc.compare(_candidate(trafficPercent=100), _twin()))

    def test_the_rule_fires_during_validation_before_anything_is_normalized(self):
        """Order is the contract. A difference caught only by the residual
        comparison is one edit away from being caught by nothing: the moment
        ``trafficPercent`` were ever paired in ``normalize``, the residual would
        stop reporting it. The rule has to hold where normalization cannot
        reach."""
        self.assertIn(CANDIDATE_TRAFFIC_REFUSAL,
                      tc._validate(_candidate(trafficPercent=100), _twin()))

    def test_the_refusal_is_the_named_rule_and_not_the_generic_residual(self):
        differences = tc.compare(_candidate(trafficPercent=100), _twin())
        self.assertTrue(
            any(RESIDUAL_MARKER not in d and "candidate carries" in d
                for d in differences),
            f"only the generic residual reported it: {differences}",
        )

    def test_the_rule_holds_when_candidate_and_twin_carry_the_same_share(self):
        """The case the residual comparison structurally cannot see.

        Both sides read 50, so after normalization there is nothing left to
        differ and the residual reports nothing at all. This input is caught by
        the named rule or by nothing.
        """
        differences = tc._validate(_candidate(trafficPercent=50),
                                   _twin(trafficPercent=50))
        self.assertIn(CANDIDATE_TRAFFIC_REFUSAL, differences)

    def test_the_residual_comparison_is_blind_to_that_same_share_input(self):
        """Proof of the sentence above, not an assertion about it.

        Normalizing that pair leaves two byte-identical specs, so every
        difference the residual loop could report is already gone. If this ever
        stops being true, the claim that the named rule is the only thing
        standing there needs re-checking -- it does not weaken the rule.
        """
        left, right = tc.normalize(_candidate(trafficPercent=50),
                                   _twin(trafficPercent=50))
        self.assertEqual(left, right)

    def test_any_nonzero_share_is_refused_not_just_a_majority(self):
        """0% is the only permitted value. A 1% candidate is already serving
        real users; the rollout would not be reversible by a stamp."""
        for share in (1, 5, 50, 100, "1", "100"):
            with self.subTest(trafficPercent=share):
                self.assertIn(
                    CANDIDATE_TRAFFIC_REFUSAL,
                    tc._validate(_candidate(trafficPercent=share), _twin()))

    def test_an_unreadable_share_is_refused_rather_than_read_as_zero(self):
        """Allowlist, not denylist. The share must be PRESENT and spelled as a
        canonical non-negative decimal; absence is not evidence of zero, and a
        value the comparator cannot read is not a value it may assume is safe.
        Refuse rather than sanitize: a coerced 0 would ship the verdict and hide
        the caller's mistake.
        """
        candidate = _candidate()
        del candidate["trafficPercent"]
        cases = {
            "missing": candidate,
            "empty string": _candidate(trafficPercent=""),
            "non numeric": _candidate(trafficPercent="fifty"),
            "none": _candidate(trafficPercent=None),
            "float": _candidate(trafficPercent=0.0),
            "bool": _candidate(trafficPercent=False),
            "negative": _candidate(trafficPercent=-1),
            "leading zero": _candidate(trafficPercent="00"),
            "padded": _candidate(trafficPercent=" 0 "),
        }
        for label, spec in cases.items():
            with self.subTest(share=label):
                self.assertIn(UNREADABLE_CANDIDATE_SHARE_REFUSAL,
                              tc._validate(spec, _twin()))

    def test_a_zero_traffic_candidate_produces_no_candidate_traffic_complaint(self):
        """Vacuity guard: a rule that always fires pins nothing."""
        problems = tc._validate(_candidate(), _twin())
        self.assertNotIn(CANDIDATE_TRAFFIC_REFUSAL, problems)
        self.assertNotIn(UNREADABLE_CANDIDATE_SHARE_REFUSAL, problems)
        self.assertNotIn(CANDIDATE_TRAFFIC_REFUSAL,
                         tc._validate(_candidate(trafficPercent="0"), _twin()))

    def test_the_candidate_rule_is_not_the_twin_rule_wearing_a_new_name(self):
        """Each side is refused on its own sentence. A candidate serving traffic
        while the twin does not is still a refusal, and the twin's rule is
        silent on it -- so neither rule can be deleted in favour of the other.
        """
        candidate_only = tc._validate(_candidate(trafficPercent=100), _twin())
        self.assertIn(CANDIDATE_TRAFFIC_REFUSAL, candidate_only)
        self.assertNotIn(TWIN_TRAFFIC_REFUSAL, candidate_only)

        twin_only = tc._validate(_candidate(), _twin(trafficPercent=100))
        self.assertIn(TWIN_TRAFFIC_REFUSAL, twin_only)
        self.assertNotIn(CANDIDATE_TRAFFIC_REFUSAL, twin_only)


if __name__ == "__main__":
    unittest.main()
