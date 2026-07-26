# Workflow Provider Reliability Build Evidence

## Decision

The fixed workflow-intent reliability mode is implemented and passes all
recorded no-effect proofs. It is ready for an explicit provider-budget
decision; no provider call is authorized or performed by this checkpoint.

## Fixed Schedule

- Mode: `workflow-reliability`
- Cases: `workflow-intents-visible`, `repeated-information-request`, and
  `unavailable-optout-suppression`
- Repeats: 4 per case
- Planned call ceiling: 12
- Conservatively reserved token ceiling: 400,000
- Conservatively reserved cost ceiling: 2,500,000 micro-USD ($2.50)
- Retries: zero
- Failure behavior: stop after the first provider-quality mismatch, policy
  blocker, semantic variance, telemetry failure, or budget refusal

The schedule is closed in code. An unexpected planned-call count fails before
provider transport construction.

## Recorded Gate

- Source revision: `32685af89ae3d190c9b9b735760cd819d24de426`
- Source tree: clean
- Result: 12/12 passed
- Gap codes: 0
- Semantic variance cases: 0
- Provider calls: 0
- Tokens: 0
- Cost: 0 micro-USD
- Selected provider-policy fixture hash:
  `c0e50aa600d1b92fc7fb52edae18dfa30765578f32b38878935b281417ff6550`
- Source tree hash:
  `613599521023beab008c93e706ec8f0894c66c1d092d546d1722341aea61b777`
- Result digest:
  `33566a2aa5f7cf8a7a0c4d5cb261f499c583194518f23f6f64d8d9867dac5462`
- Local privacy-safe report SHA-256:
  `b7e9d071a95fd6c4f0a90b6e00289eb4b473187ddb24fd4fb028a9da996d5942`

The local report remains outside source control at
`/tmp/sitesift-workflow-provider-reliability-recorded.json`.

## Regression Gate

- Provider-policy report/budget tests: 12 passed.
- Claim-pipeline suite: 330 passed in 4.918 seconds.
- Isolation suite: 6 passed in 0.079 seconds.
- Compilation: passed for the claim pipeline, bounded runner, and report tests.
- Full backend suite: 2,145 passed in 45.279 seconds.
- Diff whitespace check: passed.

## Safety Boundary

No Jill/customer data, live campaign, mailbox, browser, Google Sheet,
Firebase, Graph, OpenAI provider, deployment, merge, or production surface was
accessed. The runner still requires explicit `--allow-provider-calls`, a clean
committed source tree, an API key, and a budget transport whose limits exactly
match the stamped identity.

## Next Decision

Baylor must explicitly approve the new maximum of 12 provider calls, 400,000
conservatively reserved tokens, and $2.50 worst-case reserved spend before the
OpenAI reliability run. Approval starts only this fixed fail-fast gate; it does
not authorize effects, deployment, or production interaction.
