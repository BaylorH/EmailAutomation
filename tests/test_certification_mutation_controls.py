"""Targeted pins for the twin-deployment controls that mutation found unpinned.

Task 12 Step 4 requires each safety control to fail a TARGETED test when it is
mutated. Mutation testing found three that did not:

1. IMAGE EQUALITY. Neutering the named "image differs between candidate and
   twin" rule in ``twin_contract._validate`` killed nothing. The existing case
   only asks that *some* difference mentioning "image" comes back, and the
   generic post-normalization residual comparison supplies exactly that. The
   named rule was therefore decorative: it was leaning entirely on an adjacent
   generic check, and it would go on passing if the rule were deleted -- or the
   moment ``normalize`` ever paired ``image``, at which point nothing at all
   would be left.

2. TWIN TRAFFIC. Same shape, worse. Neutering ``twin carries production
   traffic`` killed nothing, because the residual comparison reports
   ``trafficPercent: unpaired difference`` and the existing case matches on the
   substring "traffic". The one rule saying a twin may never be a traffic
   target could be deleted with the suite green.

3. TWIN IAM DENIAL. ``scripts/deploy_certification_twin.sh`` was read by no
   test at all. Flipping ``--no-allow-unauthenticated`` to
   ``--allow-unauthenticated``, and the sole invoker binding from the operator
   service account to ``allUsers``, both left the whole suite green -- the two
   edits that would put a public, unauthenticated route on the certification
   twin.

The fix in all three cases is to assert the SPECIFIC rule rather than a
symptom that something else also produces. So these cases key on the exact
refusal text a rule emits, and on the real shipped artifact rather than a
fixture rebuilt here -- a test that constructs its own copy of a value that
exists in a real artifact is only testing itself.

Every pin below is paired with a vacuity guard, because an assertion that would
also hold if the thing it checks disappeared is not a pin.
"""

import os
import re
import shlex
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import twin_contract as tc

REPO_ROOT = Path(__file__).resolve().parents[1]
TWIN_DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_certification_twin.sh"

# The exact sentences the rules emit. Keying on these is the whole point: the
# generic residual comparison produces a DIFFERENT sentence for the same input,
# so a pin that accepted either would be pinning nothing.
IMAGE_EQUALITY_REFUSAL = (
    "image differs between candidate and twin; the twin would certify a "
    "different build"
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


class ImageEqualityIsSpecifiedNotIncidentalTests(unittest.TestCase):
    """The twin must run the candidate's exact bytes, by its own named rule."""

    def setUp(self):
        self.candidate = _candidate()
        self.twin = _twin(
            image="region-docker.pkg.dev/p/r/email-automation@sha256:" + "f" * 64)

    def test_a_baseline_candidate_and_twin_still_compare_clean(self):
        """Vacuity guard. If the fixtures did not agree to begin with, every
        assertion below would pass for the wrong reason."""
        self.assertEqual(tc.compare(_candidate(), _twin()), [])

    def test_the_named_rule_reports_a_substituted_image(self):
        """Not 'some difference mentions image' -- THIS rule, by its own text."""
        self.assertIn(IMAGE_EQUALITY_REFUSAL, tc.compare(self.candidate, self.twin))

    def test_the_rule_fires_during_validation_before_anything_is_normalized(self):
        """Order is the contract.

        A difference caught only by the post-normalization residual comparison
        is one edit away from being caught by nothing: the moment ``image`` were
        ever added to ``normalize``, the residual would stop reporting it. The
        rule has to hold at the validation stage, where normalization cannot
        reach it.
        """
        problems = tc._validate(self.candidate, self.twin)
        self.assertIn(IMAGE_EQUALITY_REFUSAL, problems)

    def test_the_refusal_is_the_named_rule_and_not_the_generic_residual(self):
        """The residual comparison also flags this input, with a different
        sentence. Accepting either sentence is what let the rule go
        decorative."""
        differences = tc.compare(self.candidate, self.twin)
        self.assertTrue(
            any(RESIDUAL_MARKER not in d and "image" in d for d in differences),
            f"only the generic residual reported it: {differences}",
        )

    def test_an_identical_image_produces_no_image_complaint(self):
        """Vacuity guard: the rule must be silent when the images agree."""
        self.assertNotIn(IMAGE_EQUALITY_REFUSAL, tc._validate(_candidate(), _twin()))


class TwinTrafficIsSpecifiedNotIncidentalTests(unittest.TestCase):
    """A twin that can receive production traffic is not a twin."""

    def test_the_named_rule_reports_a_traffic_carrying_twin(self):
        self.assertIn(TWIN_TRAFFIC_REFUSAL,
                      tc.compare(_candidate(), _twin(trafficPercent=1)))

    def test_the_rule_fires_during_validation(self):
        self.assertIn(TWIN_TRAFFIC_REFUSAL,
                      tc._validate(_candidate(), _twin(trafficPercent=1)))

    def test_the_rule_holds_even_when_the_candidate_carries_the_same_traffic(self):
        """The residual comparison sees NOTHING here -- both sides read 1 -- so
        this input is caught by the named rule or by nothing at all.

        This is the case the old test could not have distinguished, because it
        depended on the two values differing.
        """
        differences = tc.compare(_candidate(trafficPercent=1),
                                 _twin(trafficPercent=1))
        self.assertIn(TWIN_TRAFFIC_REFUSAL, differences)

    def test_any_nonzero_traffic_is_refused_not_just_a_majority_share(self):
        """0% is the only permitted value. A 1% twin is still a live target."""
        for share in (1, 5, 50, 100):
            with self.subTest(trafficPercent=share):
                self.assertIn(TWIN_TRAFFIC_REFUSAL,
                              tc._validate(_candidate(), _twin(trafficPercent=share)))

    def test_a_zero_traffic_twin_produces_no_traffic_complaint(self):
        """Vacuity guard."""
        self.assertNotIn(TWIN_TRAFFIC_REFUSAL, tc._validate(_candidate(), _twin()))


_DEFAULTED = re.compile(r"\$\{(\w+):-([^}]*)\}")
_BRACED = re.compile(r"\$\{(\w+)\}")
_BARE = re.compile(r"\$(\w+)")


def _assignments(source: str) -> dict:
    """The script's own top-level constants, read off the script.

    Resolved from the file rather than restated here on purpose: a test that
    rebuilds a value which already exists in the real artifact is only checking
    its own copy. ``${VAR:-default}`` resolves to the default, which is what an
    operator running the script with a clean environment gets.
    """
    values: dict = {}
    for name, raw in re.findall(r'^([A-Z_]+)="([^"]*)"$', source, re.M):
        values[name] = raw
    for _ in range(len(values) + 1):        # resolve chained references
        values = {name: _expand(value, values) for name, value in values.items()}
    return values


def _expand(token: str, values: dict) -> str:
    token = _DEFAULTED.sub(lambda m: values.get(m.group(1), m.group(2)), token)
    token = _BRACED.sub(lambda m: values.get(m.group(1), m.group(0)), token)
    return _BARE.sub(lambda m: values.get(m.group(1), m.group(0)), token)


def _command_array(source: str, name: str) -> list:
    """The tokens of one shell array literal in the real deploy script.

    Parsed rather than grepped so a flag cannot be satisfied by an unrelated
    mention in a comment or a printf, and so reordering the flags does not
    break the pin. Shell variables are expanded from the script's own constants,
    so the assertions below see the identities the command would really carry.
    """
    match = re.search(rf"^{re.escape(name)}=\(\n(.*?)^\)$", source,
                      re.S | re.M)
    if match is None:
        raise AssertionError(f"{name} array not found in the deploy script")
    values = _assignments(source)
    return [_expand(token, values)
            for token in shlex.split(match.group(1), comments=True)]


class TwinDeployIamDenialTests(unittest.TestCase):
    """The twin's IAM denial, read off the script that actually deploys it.

    Nothing read this file before. The two edits below -- one flag, one member
    -- are the whole distance between an IAM-private fixture-only twin and a
    publicly invokable service running the production candidate image.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = TWIN_DEPLOY_SCRIPT.read_text()
        cls.deploy = _command_array(cls.source, "deploy_command")
        cls.invoker = _command_array(cls.source, "invoker_command")

    # -- vacuity guards -----------------------------------------------------

    def test_the_parsed_commands_are_the_real_deploy_and_binding_commands(self):
        """If the parse silently produced nothing, every absence assertion in
        this class would pass while proving nothing."""
        self.assertGreater(len(self.deploy), 10, self.deploy)
        self.assertEqual(self.deploy[:4], ["gcloud", "run", "deploy",
                                           "process-user-certification"])
        self.assertEqual(self.invoker[:5],
                         ["gcloud", "run", "services", "add-iam-policy-binding",
                          "process-user-certification"])

    # -- authentication is required -----------------------------------------

    def test_the_twin_is_deployed_with_no_unauthenticated_invokers(self):
        self.assertIn("--no-allow-unauthenticated", self.deploy)

    def test_the_script_never_allows_unauthenticated_invocation(self):
        """`--allow-unauthenticated` is a prefix of the safe flag, so this is a
        token check, never a substring check."""
        self.assertNotIn("--allow-unauthenticated", self.deploy)
        self.assertNotIn("--allow-unauthenticated", self.invoker)

    def test_ingress_is_internal_so_there_is_no_public_route(self):
        self.assertIn("--ingress", self.deploy)
        self.assertEqual(self.deploy[self.deploy.index("--ingress") + 1], "internal")

    # -- exactly one principal may invoke ------------------------------------

    def test_the_only_invoker_is_the_certification_operator_service_account(self):
        self.assertIn("--member", self.invoker)
        member = self.invoker[self.invoker.index("--member") + 1]
        self.assertTrue(member.startswith("serviceAccount:"), member)
        self.assertIn("sitesift-certification-operator", member)

    def test_no_broad_principal_appears_anywhere_in_the_script(self):
        """Allowlist thinking: these two are the only spellings of 'everyone',
        and either one turns the twin into a public service."""
        for principal in ("allUsers", "allAuthenticatedUsers"):
            with self.subTest(principal=principal):
                self.assertNotIn(principal, self.deploy)
                self.assertNotIn(principal, self.invoker)

    def test_the_granted_role_is_exactly_run_invoker(self):
        self.assertIn("--role", self.invoker)
        self.assertEqual(self.invoker[self.invoker.index("--role") + 1],
                         "roles/run.invoker")

    def test_no_broad_role_is_granted(self):
        for role in ("roles/owner", "roles/editor", "roles/run.admin",
                     "roles/iam.serviceAccountTokenCreator",
                     "roles/datastore.owner", "roles/secretmanager.admin"):
            with self.subTest(role=role):
                self.assertNotIn(role, self.invoker)

    # -- the identity it runs as ---------------------------------------------

    def test_the_twin_runs_as_the_certification_runtime_service_account(self):
        """A twin running as the production identity has production authority
        no matter what its configuration says."""
        self.assertIn("--service-account", self.deploy)
        account = self.deploy[self.deploy.index("--service-account") + 1]
        self.assertTrue(account.startswith("sitesift-certification-runtime@"),
                        account)

    def test_the_image_is_offered_by_digest_and_never_built_here(self):
        """A rebuild -- even from the same commit -- is a different digest, and
        therefore a different artifact than the one under review."""
        self.assertIn("--image", self.deploy)
        self.assertRegex(self.deploy[self.deploy.index("--image") + 1],
                         r"@sha256:[0-9a-f]{64}$|TESTED_IMAGE_DIGEST")
        for verb in ("gcloud builds submit", "docker build", "docker push",
                     "git push"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.source)

    def test_the_script_never_routes_traffic(self):
        """The twin is never a traffic target, so the flags that would make it
        one are absent rather than guarded."""
        for flag in ("--to-latest", "--to-revisions", "--to-tags"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, self.deploy)


if __name__ == "__main__":
    unittest.main()
