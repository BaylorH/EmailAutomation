"""Drive the CLI against a REAL running twin over REAL HTTP.

Every layer has unit tests. This is the only place the CLI's transport, the
route's fence, the OIDC gate, the ledger, the runner and the fixture are all
exercised together through a socket -- which is the arrangement Phase E actually
deploys. Mocked-at-every-seam agreement is not evidence that the seams line up.

The server runs on loopback with a stubbed token decoder. That stub replaces
GOOGLE'S SIGNATURE CHECK ONLY; every claim the route checks -- issuer, audience,
operator address, numeric subject, email_verified, expiry -- is checked for real
against the real code path, and a RED here presents a wrong operator and
requires 401.
"""

import json
import os
import threading
import unittest
from unittest.mock import patch
from wsgiref.simple_server import WSGIServer, make_server

os.environ.setdefault("E2E_TEST_MODE", "true")

import service
from email_automation.certification import ledger as ledger_module
from email_automation.certification import lifecycle

REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
OPERATOR = "sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com"
SUB = "104729384756102938475"


class _QuietServer(WSGIServer):
    def handle_error(self, request, client_address):
        pass


class CliAgainstLiveTwinTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, service.app, server_class=_QuietServer)
        cls.port = cls.server.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "scripts" / "certify_production.py"
        spec = importlib.util.spec_from_file_location("certify_production", path)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

        self.env = {
            "K_SERVICE": "process-user-certification",
            "K_REVISION": "process-user-certification-00001-abc",
            "SITESIFT_SOURCE_REVISION": REVISION,
            "SITESIFT_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "SITESIFT_PRODUCTION_CANDIDATE_REVISION": "process-user-00042-xyz",
            "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION": "7",
            "SITESIFT_FIXTURE_CONFIG_DIGEST": "d" * 64,
            "SITESIFT_CERTIFICATION_AUDIENCE": self.url,
            "SITESIFT_CERTIFICATION_OPERATOR_EMAIL": OPERATOR,
            "SITESIFT_CERTIFICATION_OPERATOR_SUB": SUB,
        }
        self.enter = patch.dict(os.environ, self.env, clear=False)
        self.enter.start()
        self.addCleanup(self.enter.stop)
        self.ledger = patch.object(lifecycle, "_DEFAULT_LEDGER",
                                   ledger_module.InMemoryRunLedger())
        self.ledger.start()
        self.addCleanup(self.ledger.stop)

    def _decoder(self, email=OPERATOR, sub=SUB):
        return lambda token, audience: {
            "iss": "https://accounts.google.com", "aud": self.url,
            "email": email, "email_verified": True, "sub": sub,
            "exp": 4102444800,
        }

    def _authenticated(self, **kwargs):
        return patch.object(service, "_caller_decoder", self._decoder(**kwargs))

    # -- the whole run, over a socket --------------------------------------

    def test_the_cli_drives_a_full_certification_run_over_http(self):
        run_id = self.cli.new_run_id("campaign-one-property", nonce="live-1")
        body = {"scenarioId": "campaign-one-property", "runId": run_id,
                "expectedRevision": REVISION}

        with self._authenticated():
            prepared, code = self.cli.call(self.url, "prepare", body, token="t")
            self.assertEqual(code, 200, prepared)
            self.assertEqual(prepared["state"], "PREPARED")

            result, code = self.cli.call(self.url, "run", body, token="t")
            self.assertEqual(code, 200, result)

            final, code = self.cli.call(self.url, "status",
                                        {"runId": run_id,
                                         "expectedRevision": REVISION}, token="t")

        self.assertEqual(result["verdict"], "PASS")
        self.cli.assert_verdict_is_supported(result)
        self.assertEqual(result["counts"]["captured_outreach"], 1)
        self.assertEqual(result["counts"]["graph_network"], 0)
        self.assertEqual(result["counts"]["replay_delta"], 0)
        self.assertEqual(result["counts"]["cleanup_residue"], 0)
        self.assertEqual(final["state"], "TERMINAL")
        self.assertEqual(final["verdict"], "PASS")

    def test_a_wrong_operator_is_rejected_over_the_socket(self):
        """The stub replaces the signature check only. Everything else is real."""
        body = {"scenarioId": "campaign-one-property",
                "runId": self.cli.new_run_id("campaign-one-property", nonce="live-2"),
                "expectedRevision": REVISION}
        with self._authenticated(email="attacker@example.invalid"):
            payload, code = self.cli.call(self.url, "prepare", body, token="t")
        self.assertEqual(code, 401)
        self.assertEqual(payload["reason"], "unauthorized")

    def test_an_unauthenticated_call_is_rejected_over_the_socket(self):
        body = {"scenarioId": "campaign-one-property",
                "runId": self.cli.new_run_id("campaign-one-property", nonce="live-3"),
                "expectedRevision": REVISION}
        payload, code = self.cli.call(self.url, "prepare", body, token="")
        self.assertEqual(code, 401)

    def test_a_replayed_run_id_is_rejected_over_the_socket(self):
        run_id = self.cli.new_run_id("campaign-one-property", nonce="live-4")
        body = {"scenarioId": "campaign-one-property", "runId": run_id,
                "expectedRevision": REVISION}
        with self._authenticated():
            self.cli.call(self.url, "prepare", body, token="t")
            self.cli.call(self.url, "run", body, token="t")
            _payload, code = self.cli.call(self.url, "run", body, token="t")
        self.assertEqual(code, 409)

    def test_the_ordinary_production_route_is_inert_on_the_twin_over_http(self):
        """The fence, proved through a socket rather than a test client."""
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            f"{self.url}/process-user",
            data=json.dumps({"uid": "real-user"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with patch("service.refresh_and_process_user") as pipeline:
            try:
                urllib.request.urlopen(request, timeout=30)
                self.fail("the twin served an ordinary production route")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404)
        pipeline.assert_not_called()

    def test_no_fixture_value_crosses_the_socket(self):
        run_id = self.cli.new_run_id("campaign-one-property", nonce="live-5")
        body = {"scenarioId": "campaign-one-property", "runId": run_id,
                "expectedRevision": REVISION}
        with self._authenticated():
            prepared, _ = self.cli.call(self.url, "prepare", body, token="t")
            result, _ = self.cli.call(self.url, "run", body, token="t")
        blob = json.dumps([prepared, result], sort_keys=True)
        for forbidden in ("@", "broker", "Hi Pat", "100 Fixture Way", "cert-uid-0001"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} crossed the socket")


if __name__ == "__main__":
    unittest.main()
