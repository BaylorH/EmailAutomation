# Workflow-Intent Action Closure Evidence

## Decision

The deterministic claim-to-policy layer now preserves accepted tour and
information requests as explicit human-owned actions. This closes both named
workflow gaps from the bounded provider-to-policy shadow.

This evidence supports the policy contract. It does not authorize an effect
adapter, deployment, production traffic, or automatic replies.

## Behavior Proved

- `tour_request` and `information_request` actions require human approval.
- Each action must cite a matching accepted claim and accepts only bounded
  workflow notes.
- Pure tour or information requests enter review and produce a typed action
  plus the existing review queue item.
- Repeated information requests collapse to one typed action while retaining
  all matching source-claim IDs.
- Opt-out or another terminal condition retains precedence: follow-ups freeze,
  conversation state remains terminal, and request actions stay visible
  without reopening outreach.
- No policy result creates an outbound draft.

## Recorded Composition Gate

- Source revision: `209522ed8b4b2bbff123d6356644c14eef6dd345`
- Source tree: clean
- Mode: fixed recorded final, 8 cases x 3 repeats
- Result: 24/24 passed
- Gap codes: 0
- Semantic variance cases: 0
- Provider calls: 0
- Tokens: 0
- Cost: 0 micro-USD
- Result digest:
  `c2fa92614e36f0749161ebaca5ab88d8617b844fe93199b5b4b6d86c1d253b08`
- Provider-policy fixture hash:
  `83ec0f4c0a0f14eb22e189acbd457513c3230e2bb48dd5ee734f08d3452c6bc0`
- Local privacy-safe report SHA-256:
  `4bbc79cf58725b54ce3852177fb2745d2bff25573798fb539c11e1dda9c691a1`

The local report remains outside source control at
`/tmp/sitesift-workflow-intent-recorded-final.json`.

## Regression Gate

- Claim-pipeline suite: 326 tests passed in 4.199 seconds.
- Isolation suite: 6 tests passed in 0.081 seconds.
- Compilation: passed for the claim pipeline, bounded runner, and changed
  tests.
- Full backend suite: 2,141 tests passed in 47.335 seconds.
- Diff whitespace check: passed.
- Effect-surface scan: no new Firebase, Firestore, Graph, Sheets, mailbox,
  send, or service imports.

## Safety Boundary

No Jill/customer data, live campaign, mailbox, browser, Google Sheet,
Firebase, Graph, OpenAI provider, deployment, merge, or production surface was
accessed. Test logs that describe sending or sheet writes are mocked regression
scenarios from the existing backend suite.

The previous provider budget is exhausted and was not reused. A fresh provider
reliability gate must set a new explicit call, token, and spend cap before any
additional model call.

## Remaining Gates

1. Run a newly capped provider reliability gate against the updated exact
   policy oracles and investigate any intermittent extraction mismatch.
2. Only after that gate passes, design a disabled effect adapter for typed
   actions and prove idempotency, stale-state rejection, and no-send behavior.
3. Prove staging persistence and read-only admin visibility.
4. Run browser-driven staging campaign workflows at small, medium, and large
   row counts with exact usage math and failure recovery.
5. Require explicit approval before any production canary, merge, deployment,
   or live-user interaction.
