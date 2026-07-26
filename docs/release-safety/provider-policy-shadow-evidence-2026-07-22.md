# Provider-to-Policy Shadow Evidence - 2026-07-22

## Scope

This evidence covers the no-effect evidence -> provider claims -> deterministic
policy path only. It used sanitized fixtures and did not read or mutate a live
campaign, mailbox, sheet, customer record, browser session, follow-up, outbox,
or production service.

The pinned claim prompt is the proposed claim-extraction boundary. It is not the
deployed legacy `updates` / `events` / `response_email` prompt.

## Implementation Findings

The composition work found and fixed two deterministic integration defects:

- correction audit claims can now reference the domain claim they explain
  without allowing ordinary cross-predicate supersession;
- structured referral claims now use their explicit email for the
  human-required recipient-change action instead of stringifying the whole
  referral object.

The shadow also gained strict fixtures, exact policy grading, independent
provider telemetry, conservative pre-call reservations, one/24-call fixed
modes, privacy-safe diagnostics, and fail-fast behavior for extraction failure
or semantic repeat variance.

## Pinned Identity

- Final harness revision: `643831e4d86f6b72a0aea8fe50a1f376ebaa2bc8`
- Final source-tree hash: `ec5be60ad5e32644739b8c12476ea0f000335c463c40d386e6e0bee2ef5c2ccc`
- Model: `gpt-5.2-2025-12-11`
- Prompt: `sitesift-claim-proposal-2026-07-22-v7`
- Prompt hash: `610e107ff36cd2e856977543cc3b61a75f31642e56694ce9f9191d7b56fb2477`
- Claim fixture hash: `676e3b1b4208bb676069bf5d282d086d9b35980f19f099a49305ec059a987097`
- Provider-quality fixture hash: `9b4f9f70461eb2d7f5d8582b36a7f5ec0d20b20c89a1e23f92fd6d2fbb8b8190`
- Reconciliation manifest hash: `cea647e725eec2cc4627b25f9e84139c46ec8d4cd036ab7e9ac5fb40131fe6a8`

## Provider Sequence

| Run | Calls | Result | Observed tokens | Cost (micro-USD) |
| --- | ---: | --- | ---: | ---: |
| Terminal/opt-out smoke | 1 | Passed | 2,544 | 7,135 |
| Three-repeat final attempt | 15 | Stopped on workflow provider-quality failure; 14 passed | 43,737 | 124,382 |
| Workflow diagnostic | 1 | Passed | 3,121 | 11,345 |
| Six-case reconciliation | 6 | Passed | 18,135 | 45,883 |
| Attachment reconciliation | 2 | Passed | 5,166 | 4,502 |
| **Total** | **25** | **24 clean samples; 1 failed provider-quality call** | **72,703** | **193,247** |

The final attempt stopped at 15 of 24 planned calls. The failing call was the
second workflow-intent sample. Nine later evaluations were not called. The
pre-diagnostic report preserved only `provider_quality_failed`, so the more
specific extraction mismatch category is unavailable. Privacy-safe detailed
provider mismatch codes are preserved by the harness after revision `643831e`.

Two subsequent workflow-intent calls passed with the expected semantic digest.
This makes the failure intermittent, not a deterministic policy mismatch.

## Reconciled Semantics

The bounded 25-call authorization produced exactly three clean samples for each
of eight cases. Each case had one policy outcome digest across its three clean
samples:

| Case | Clean samples | Disposition | Policy outcome digest |
| --- | ---: | --- | --- |
| Attachment alternate isolation | 3 | Pass | `393655ec4a8d5cfa482f51f326103004be12a3302dd60888e75a14ed09df190b` |
| Complete facts closeout | 3 | Pass | `ceee3b681e4c7fc309e804d16e2748fcd6d6d4886170160ff751997b335f762b` |
| Fresh suite closeout | 3 | Pass | `63bb2ac4b94d1c53906dc233b3338f0f502530f039c1a37f8f49dd6d29df6652` |
| Rent correction closeout | 3 | Pass | `467374910f2524856c7e352202e0f9c2697e56b24b9954dd4b3f12bdde1fd5b7` |
| Repeated information request | 3 | Expected gap | `6529d907e901d01a88bc745f320796ee4b880fc902a92abac4c7ebe2425213f4` |
| Split-suite isolation | 3 | Pass | `4b4959763d16f806354f06a533bf2c044d1e9eec2ec45808d2e33e0fc8b5fcd7` |
| Unavailable plus opt-out suppression | 3 | Pass | `0a621492a3972e843af4a835bd276bbb57dec6232ee8017e98679d9bb0252adc` |
| Workflow intents | 3 | Expected gap | `90a9f519fbf386ed0620e931c8deeedde702daa68828736795b8da05957ce0fa` |

## Known Gaps

- `information_request_action_missing`: brochure/information requests are
  extracted but do not yet create an explicit policy action.
- `tour_request_action_missing`: tour requests are extracted but do not yet
  create a human-required tour action.
- Workflow-intent provider extraction had one intermittent quality failure in
  four attempts during this gate. The original uninterrupted three-repeat gate
  therefore did not pass, even though the reconciled corpus has three clean
  samples per case.

## Verification

- Focused provider-policy, replay, policy, validation, contracts, and isolation
  suites passed.
- Full backend suite: **2,138 tests passed in 44.449 seconds**.
- Compilation checks passed.
- Final application worktree was clean before every provider-backed run.
- Provider reports contained no evidence text, message bodies, addresses,
  emails, recipients, claim values, or raw model output.

## Gate Status

The composition architecture is strongly supported but the provider gate is
not an uninterrupted pass. Do not start the effect adapter yet.

The next gate should first add explicit information-request and tour-request
actions, then isolate workflow-intent extraction stability with the new safe
mismatch diagnostics. Only a clean bounded repeat can unlock the disabled
effect-adapter design.
