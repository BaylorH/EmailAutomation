# Production certification stamps — schema and retention contract

A **stamp** is the only artifact that may claim a SiteSift business capability works
in production. This directory documents the stamp shape. It deliberately holds no
stamp: a dynamic stamp is **never committed** to the product branch. Stamps are
retained **private** and sanitized.

## Why no stamp is committed here

A stamp binds the exact source SHA it certifies. Committing a stamp into the same
repository changes that repository's SHA, so the stamp would certify a revision
that no longer exists the instant it is recorded. Durable stamps therefore live in
the **private** certification ledger, with a sanitized checkpoint mirrored to Brain.

## Retention contract

Retained stamp evidence is **sanitized** and revision-bound. A stamp may record only:

- verdict, capability id, scenario ids, and exact required/forbidden effect counts;
- the bound identity described by `../identity.schema.json`;
- run ids, repeat index, and timestamps;
- digests — prompt digest, canonical input digest, evidence digest, registry digest;
- the logical fixture and oracle projection aliases.

A stamp must **never** contain, and this is not a preference:

- a fixture value, recipient, mailbox address, or any other PII;
- a Sheet id, Drive id, thread id, client id, or other concrete resource identity;
- a secret, token, credential, or fixture-config secret payload;
- a raw exception, raw stdout, or raw provider response body;
- a raw naturalness-review body — only the body digest and rubric version.

Concrete identities come only from the bound immutable numeric fixture-config
secret at execution time. They are resolved in memory and never serialized.

## Valid verdicts

| Verdict | Meaning |
| --- | --- |
| `PASS` | Every required effect observed at its exact cardinality, every forbidden effect at zero, replay produced zero delta, cleanup left zero residue. |
| `FAIL` | The capability did not hold. Recorded, ranked, and never quietly retried. |
| `INSTRUMENT_BLOCKED` | The instrument could not measure the capability. Never a pass and never a failure of the product. |
| `NOT_TESTED` | No production-resident evidence exists. Every capability starts here. |

HTTP success, a health check, a source review, a localhost test run, a
candidate-only result at zero percent traffic, and historical provider evidence
from an earlier revision **never** satisfy current-production completion.

## Invalidation

A stamp is invalidated when any bound identity field changes, or when a production
path it declared is modified. Invalidation is recomputed by
`scripts/rank_certification_frontier.py`; it is never asserted by hand.
