# Workflow Provider Reliability Evidence

## Decision

The fixed workflow-intent provider reliability gate passed. All 12 approved
provider samples matched both the exact claim-quality oracle and the exact
deterministic policy/action oracle, with no gap, mismatch, or semantic
variance.

This closes the focused provider-reliability prerequisite for designing a
disabled effect adapter. It does not authorize effects, deployment, production
traffic, or live-customer testing.

## Authorized Boundary

- Maximum calls: 12
- Maximum conservatively reserved tokens: 400,000
- Maximum conservatively reserved cost: 2,500,000 micro-USD ($2.50)
- Retries: zero
- Failure behavior: stop after the first provider-quality mismatch, policy
  blocker, semantic variance, telemetry failure, or budget refusal

The run used the committed `workflow-reliability` schedule: compound workflow
intents, repeated information request, and unavailable/opt-out control, each
repeated four times.

## Provider Result

- Source revision: `6cd4567f4b840a93b45c6cf48e157526488835da`
- Source tree: clean
- Provider: OpenAI
- Model: `gpt-5.2-2025-12-11`
- Prompt: `sitesift-claim-proposal-2026-07-22-v7`
- Prompt hash:
  `610e107ff36cd2e856977543cc3b61a75f31642e56694ce9f9191d7b56fb2477`
- Result: 12/12 passed
- Provider billed calls: 12
- Gap codes: 0
- Mismatch codes: 0
- Semantic variance cases: 0
- Input tokens: 29,328
- Output tokens: 4,390
- Total tokens: 33,718
- Actual cost: 80,733 micro-USD ($0.080733)
- Conservatively reserved tokens: 273,416 / 400,000
- Conservatively reserved cost: 1,654,484 / 2,500,000 micro-USD
- Observed provider latency: 56,901 ms
- Result digest:
  `33566a2aa5f7cf8a7a0c4d5cb261f499c583194518f23f6f64d8d9867dac5462`
- Selected provider-policy fixture hash:
  `c0e50aa600d1b92fc7fb52edae18dfa30765578f32b38878935b281417ff6550`
- Source tree hash:
  `613599521023beab008c93e706ec8f0894c66c1d092d546d1722341aea61b777`
- Local privacy-safe report SHA-256:
  `fc50950f35dde47973158b98ff08dea5a76624c643bd0816f137092436801043`

The local report remains outside source control at
`/tmp/sitesift-workflow-provider-reliability-openai-approved.json`.

## Interpretation

The earlier one-of-four compound-workflow failure did not reproduce across
four new compound-workflow attempts or either opposed case. Within this fixed
gate, the provider consistently preserved referral, call, tour, information,
remediation, return-date, opt-out, repeated-request, and terminal-suppression
semantics.

The evidence supports classifying the earlier miss as intermittent provider
variance rather than a currently reproducible deterministic-policy defect. It
does not prove that the provider can never vary; fail-closed validation,
human-owned workflow actions, and runtime mismatch observability remain
required.

## Regression Gate

- Preflight provider-policy/report/isolation tests: 24 passed.
- Post-run claim-pipeline suite: 330 passed in 4.337 seconds.
- Post-run full backend suite: 2,145 passed in 47.664 seconds.
- Compilation: passed for the claim pipeline and bounded runner.
- Report assertions: exact caps, complete usage, 12/12 results, zero gaps,
  zero mismatches, zero variance, and budget compliance passed.
- Report privacy scan: no fixture addresses, email addresses, evidence text,
  raw output, or recipient fields found.

## Safety Boundary

`caffeinate` was attached to the bounded Python process and ended when the run
ended. No Jill/customer data, live campaign, mailbox, browser, Google Sheet,
Firebase, Graph, queue, draft, send, deployment, merge, or production surface
was accessed.

## Next Gate

Design a disabled-by-default effect adapter against the proven typed action
contract. Before connecting any service, prove idempotency, stale-snapshot
rejection, authorization boundaries, dry-run receipts, terminal suppression,
and zero-send behavior. Staging persistence and browser-visible Admin evidence
remain later gates; production remains separately approved.
