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
and overlap guards before ranking candidates. Current evidence includes explicit
correction/corrected/correct/actually discourse as well as current/now/revised/
updated markers. Field inheritance is syntax-bounded: an expense-owned prior
rate can govern an unlabeled replacement only in `, corrected to`, `, now`,
`; correction:`, or `; actually` form, while a pronominal replacement can
inherit either field only in `not $old...; it is $new...`. Both figures must be
dollar rates with recognized per-area units, and their complete optional
basis/NNN suffixes stay attached. Unrelated later asking or lease rates do not
inherit OpEx ownership. For a correction candidate, basis resolution is confined
to the replacement figure's captured `current_evidence` span, so the prior
figure's monthly or annual basis cannot contaminate the replacement. When
equally specific current candidates occur in sequence, the later correction
wins. `_ops_ex_winner` selects once.
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

For bounded explicit-expense and correction forms, one owned rate span consumes
the per-area unit together with an optional attached basis suffix and optional
trailing `NNN`. Captured compositions include `per SF/month NNN`,
`per SF per month NNN`, and `per SF/year NNN`. Monthly forms annualize before
extraction and proposal normalization, while annual forms retain their raw
value. Explicit expense owners such as `Expenses`, `Operating expenses`,
`Operating costs`, `CAM`, and `Pass-throughs` admit `per SF NNN` and
`per square foot NNN` figures without duplicating them into Rent.

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
By contrast, a coordinated `for [property|real estate] taxes and insurance`
list after an explicit OpEx rate is a supporting component qualifier in either
tax-to-insurance or insurance-to-tax order, including after bare `monthly`.
Direct tax or insurance fields remain competing, as do asking/quoted rates and
lease/asking prices. Conflicting candidate-owned monthly and annual markers make
an accepted candidate abstain.

### Ambiguous NNN ownership

Figure-first `NNN` is ambiguous because it can describe rent basis or operating
expenses. `_figure_field_owner` is the shared span-bounded resolver used by rent
and OpEx admission; `_nnn_figure_owner` exposes the same decision to NNN negative
evidence. It classifies a figure as `rent`, `opex`, `neutral`, or `conflict`
without using magnitude.

- Asking/rent/rate, offer/offered/offering, available/availability, and area
  syntax owns the figure as rent; explicit separators include word `at`, `for`,
  `@`, colon, and typographic dashes. Safe rate modifiers include approximately,
  `approx.`, about, around, and roughly.
- An explicit expense noun, CAM, OpEx, TMI, or an immediate same-field
  `/CAM`, `/OpEx`, or `/TMI` suffix owns it as OpEx.
- Explicit field nouns ordinarily outrank contextual offer/area syntax. In the
  captured pending/TBD exception, a later Availability/offer governor supersedes
  the expense owner only across `|`, `:`, `-`, `–`, or `—`; that later figure is
  rent-owned. Semicolon, period, and newline boundaries begin a new ownership
  clause, while direct `CAM: $... NNN` remains OpEx-owned. Relational
  objects after before/excluding/net-of/does-not-include language do not displace
  the governing subject, and a coordinated `X is separate and Y is $...` clause
  is owned by `Y`. Direct same-figure rent/expense co-ownership remains a
  conflict.
- Every generic `/SF ... NNN` rent path uses the same owner gate. A real explicit
  rate unit can preserve established figure-first rent shorthand, while expense-
  owned NNN is OpEx-only and unrelated `rate` substrings in words such as
  `separate`, `corporate`, or `accurate` never create rent ownership.
- A recognized second addend remains owned by the combined-expression source.
- Bare `$3.65 NNN` is neutral.
- Conflicting ownership such as `Asking $14.10 NNN/CAM` abstains.

This classification is shared by rent extraction, OpEx candidate admission, and
proposal validation. Clear `$3.65 CAM`, `$3.65 OpEx`, `$3.65 TMI`, and
expense-owned `$3.65 NNN` forms remain supported.

### Negative evidence and proposal-write safety

Rejected combined totals, combined-equation base-rent figures, non-expense NNN
figures, and explicitly negated per-area rates are not accepted
`_OpsExCandidate` records. A rate immediately governed by `not` is excluded from
both Rent and OpEx extraction and retained as separate negative evidence with
its numeric span plus raw and annualized values. Combined-equation base rent
inherits the recognized equation basis without entering the accepted winner
set. Negated-rate proposal sanitation applies that evidence symmetrically to
preseeded Rent and OpEx writes: it removes an exact rejected value only when the
destination field lacks independent support for the same normalized raw or
annualized value. Independently supported values survive. The ownership gate
also prevents an expense-only value from surviving in Rent or a rent-only value
from surviving in OpEx, and explicitly monthly rent contributes both its raw
and annualized values to the OpEx rejection set.

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

- Offer/offered/offering, available/availability, and area `$14.10 NNN`
  shorthand using word `at`, `for`, `@`, colon, or typographic dashes extracts
  rent `14.10`, never OpEx, including with safe approximation modifiers, and
  removes a matching model-proposed OpEx value during full augmentation.
- In the captured pending/TBD exception, an expense owner yields to a later
  Availability/offer governor only across `|`, `:`, hyphen, en dash, or em dash;
  direct `CAM: $... NNN` remains expense-owned, and semicolon, period, and
  newline boundaries start a new ownership clause.
- Explicit expense-owned NNN/CAM/OpEx/TMI forms remain eligible; bare NNN is
  neutral, explicit `/SF NNN` never duplicates into rent, governing relational
  and coordinated subjects remain symmetric, and direct conflicting ownership
  abstains.
- Explicit expense-owned `per SF NNN` and `per square foot NNN` figures, plus
  composed `per SF/month NNN`, `per SF per month NNN`, and `per SF/year NNN`
  forms, retain their owner and basis through extraction and proposal
  normalization without duplicating into Rent.
- All accepted combined-equation and standalone monthly forms annualize `0.34`
  to `4.08` in both extraction and proposal normalization.
- Punctuated or multiword following subjects and unrelated rent, parking, tax,
  or insurance fields do not contaminate candidate basis; an attached
  coordinated tax-plus-insurance qualifier remains supporting OpEx context in
  either order, while asking/quoted rates and lease/asking prices compete.
- Correction/corrected/correct/actually discourse selects the later OpEx value;
  bounded elliptical OpEx and pronominal `not $old...; it is $new...` forms
  inherit only their prior field and retain complete `/month`, `per month`,
  `/year`, or `NNN` suffixes. Basis comes only from the current replacement
  figure, unrelated later rates remain separate, and sequential corrections
  select the latest equally specific current candidate.
- Explicitly negated rates are excluded from both extractors and sanitized
  symmetrically from preseeded proposals unless independently supported in the
  destination field.
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
