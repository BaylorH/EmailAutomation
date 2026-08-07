# Evidence-Bounded OpEx Precedence Correction

Date: 2026-08-07

Status: implemented locally within the active SiteSift production-readiness build

## Production finding and design correction

The Wave B-M1 broker reply stated rent as `$14.10 NNN` and later stated that
`CAM, taxes, and insurance are running roughly $3.90 per square foot`. The
deployed deterministic fallback duplicated the rent figure into `Ops Ex / SF`.

The first correction bounded the broad matcher, but later review exposed a
second split-brain path: extraction and proposal normalization inferred basis
independently. Nearby rent, parking, report, or annual language could therefore
change an OpEx value even when it did not belong to that value. The final design
uses one accepted-candidate model and keeps negative evidence separate.

## Architecture

### Accepted candidate and winner

Every accepted OpEx candidate is represented by `_OpsExCandidate` with:

- the raw `Decimal` value;
- the annualized `Decimal` value;
- its owned monthly or annual basis;
- numeric and owned-evidence spans;
- source (`combined`, `narrow`, or `legacy`); and
- explicit precedence.

`_ops_ex_candidates` applies recency, hypothetical, combined-total, field-owner,
and overlap guards before ranking candidates. `_ops_ex_winner` selects once.
Both `_extract_ops_ex_sf_from_text` and `_augment_proposal_opex_basis` consume
that same winner from the same fresh, quote-stripped inbound text. Proposal
normalization changes only a value equal to the winning monthly candidate's raw
value; an already annualized or unrelated value is unchanged, making repeated
normalization idempotent.

### Basis ownership

`_ops_ex_basis_values` owns basis syntax only when it is structurally attached
to the candidate. Supported forms include `/mo`, `/month`, `per month`, bare
`monthly`, `billed monthly`, and `billed on a|the monthly basis`, together with
annual equivalents. Rate units include `PSF`, `/SF`, `per SF`, `per-SF`,
`sq. ft.`, `sq.ft.`, `sq ft.`, and `square foot`.

Combined equations remain the most specific source. Their matcher consumes the
equation total and unit so a 30-character trailing ownership window still covers
forms such as:

```text
$1.25 NNN + $0.34 OPEX = $1.59/SF/month
$1.25 NNN + $0.34 OPEX = $1.59 per square foot, billed monthly
```

Decimal points and recognized square-foot abbreviation periods are not clause
boundaries. Real sentence punctuation remains a boundary. A marker followed by
a subject, including `monthly-report`, `monthly: rent`, `monthly - rent`,
`monthly (rent ...)`, `per year parking`, or similar field language, is not
owned by the OpEx candidate. Direct multiword fields such as `property taxes`,
`real estate taxes`, and `property insurance` are likewise competing subjects.
By contrast, `for taxes and insurance` after an explicit OpEx rate is a
supporting component qualifier, including after bare `monthly`. Conflicting
candidate-owned monthly and annual markers make an accepted candidate abstain.

### Ambiguous NNN ownership

Figure-first `NNN` is ambiguous because it can describe rent basis or operating
expenses. `_nnn_figure_owner` classifies it as `rent`, `opex`, `neutral`, or
`conflict` from explicit field ownership; magnitude never decides the field.

- Asking/rent/rate, offer-at, available-at, and area-at syntax owns the figure as
  rent.
- An explicit expense noun, CAM, OpEx, TMI, or an immediate same-field
  `/CAM`, `/OpEx`, or `/TMI` suffix owns it as OpEx.
- A recognized second addend remains owned by the combined-expression source.
- Bare `$3.65 NNN` is neutral.
- Conflicting ownership such as `Asking $14.10 NNN/CAM` abstains.

This classification is shared by rent extraction, OpEx candidate admission, and
proposal validation. Clear `$3.65 CAM`, `$3.65 OpEx`, `$3.65 TMI`, and
expense-owned `$3.65 NNN` forms remain supported.

### Negative evidence and proposal-write safety

Rejected combined totals, combined-equation base-rent figures, and non-expense
NNN figures are not accepted `_OpsExCandidate` records. They remain separate
negative evidence with their numeric spans plus raw and annualized values.
Combined-equation base rent inherits the recognized equation basis without
entering the accepted winner set. Proposal validation removes only an exact
rejected value that is not also supported by an accepted candidate.

Combined-total rejection is intentionally stronger than candidate acceptance:
negative evidence survives an ambiguous or conflicting basis. If the owned
basis context contains any monthly token, both raw and conservative x12 values
are rejected regardless of token order or which suffix the outer evidence regex
captured. Thus combined totals ending in `/month/year`, `/year/month`,
`per year/month`, `/year per month`, or `/year, billed monthly` reject both
`1.50` and `18.00`. A conflicting combined equation likewise rejects base rent
`1.25` and `15.00` even when candidate acceptance abstains. In a recognized
monthly equation, legitimate OpEx `0.34` and `4.08` remain protected by accepted
evidence. Unrelated `per year parking` cannot erase a rejection. This check runs
before terminal-event early returns.

## Acceptance criteria

- Offer-at and available-at `$14.10 NNN` shorthand extracts rent `14.10`, never
  OpEx, and removes a matching model-proposed OpEx value during full augmentation.
- Explicit expense-owned NNN/CAM/OpEx/TMI forms remain eligible; bare NNN is
  neutral and conflicting rent/expense ownership abstains.
- All accepted combined-equation and standalone monthly forms annualize `0.34`
  to `4.08` in both extraction and proposal normalization.
- Punctuated or multiword following subjects and unrelated rent, parking, tax,
  or insurance fields do not contaminate candidate basis; an attached
  `for taxes and insurance` qualifier remains supporting OpEx context.
- Rejected combined totals remove both raw and annualized proposal values even
  with terminal events, unrelated annual fields, or monthly/annual conflicts in
  either order; combined-equation base rent receives the same negative evidence.
- Jill, the focused five-file backend suite, the release-critical suite, syntax
  compilation, diff checks, and the clean-worktree check pass.

## Scope and release boundary

Only `email_automation/ai_processing.py`, focused regression tests, and these
planning artifacts change. Recipient, outbox, access, notification, scheduler,
and deployment behavior are out of scope. This is a local no-ship correction:
no push, deploy, or external action is authorized.
