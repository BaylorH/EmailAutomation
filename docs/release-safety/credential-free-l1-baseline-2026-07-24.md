# Credential-Free L1 Baseline Evidence

Date: 2026-07-24

Source commit tested:
`c4fac5042d9c077754ab1321616ca6c490680763`

Branch:
`codex/sitesift-test-harness-registry-20260724`

## Scope

This record covers the credential-free L1 backend baseline and the explicit
availability result for L2, L3, and L4. It does not prove emulator, sandbox
service, deployed, browser, or live-campaign behavior.

Gate 2 remains unauthorized.

## Canonical Command

```bash
./scripts/run_test_level.sh --level L1
```

The repository wrapper used `uv --isolated` with `requirements.lock`; it did
not depend on a virtual environment from another checkout.

For the recorded run, sentinel values were deliberately supplied for Google,
Microsoft, OpenAI, and Firebase credential variables before starting the
canonical command. The L1 boundary test confirmed those inherited values were
absent during both unittest discovery and execution.

## Enforced L1 Boundary

- `E2E_TEST_MODE=true` is active only during discovery and execution.
- Inherited environment names containing credentials, secrets, passwords,
  private keys, bearer values, API keys, or tokens are removed and restored.
- Sensitive environment values created during a test are removed before the
  caller's original environment is restored.
- Known provider client-ID and emulator environment names are also removed.
- Google Cloud and Firebase Admin Firestore client factories return an
  in-memory `MagicMock`.
- Firebase Admin initialization returns an in-memory `MagicMock`.
- MSAL public/confidential client factories and the OpenAI client factory
  return in-memory `MagicMock` instances.
- TCP connection helpers, UDP/datagram send operations, and standard DNS
  resolution functions raise `L1NetworkAccessBlocked`.
- No `service-account.json` or root test token-cache file was present.

The first network-blocked candidate run exposed an import-time MSAL tenant
discovery call in `test_surface_c_device_flow`. MSAL client construction was
then added to the same test-only replacement boundary before this recorded
green run.

## Recorded Result

```text
L1 PASSED tests=2294 failures=0 errors=0 skipped=0
real 52.06
user 17.09
sys 4.04
```

The full discovery pattern was `tests/test*.py`. Application output was
suppressed. Assertion and loader failures use exit code `1`.

## Explicit Higher Levels

The same committed source returned exit code `3` for each unavailable
environment:

```text
L2 UNAVAILABLE: missing required environment: FIRESTORE_EMULATOR_HOST
L3 UNAVAILABLE: missing required environment: SITESIFT_L3_SANDBOX
L4 UNAVAILABLE: missing required environment: SITESIFT_L4_APPROVED
```

No emulator, provider sandbox, controlled E2E campaign, production credential,
deployment, persistence, API, UI, worker integration, or live service action
was attempted.
