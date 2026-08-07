# Evidence-Bounded OpEx Precedence Correction

Date: 2026-08-07

Status: approved within the active SiteSift production-readiness build

## Production finding and audit correction

The Wave B-M1 broker reply stated an asking rent of `$14.10 NNN` and later stated
`CAM, taxes, and insurance are running roughly $3.90 per square foot`. The
deployed deterministic fallback returned `14.10`, duplicating rent into
`Ops Ex / SF`.

The first hotfix added an arbitrary 96-character keyword-first matcher. Audit
rejected that architecture: it repaired the production sentence but admitted
nine plausible non-OpEx figures that the deployed extractor had correctly left
alone. A negative extractor result was also insufficient because proposal
augmentation retained a model-proposed OpEx update unless deterministic evidence
provided a replacement.

## Considered approaches

1. **Evidence-bounded positive and negative grammars — selected.** Add only the
   two explicit positive structures required by production evidence, detect
   combined rent-and-OpEx totals separately, and use the negative evidence both
   while extracting and while validating an existing model proposal.
2. **Keep the broad matcher and enumerate exclusions — rejected.** Each new
   exclusion leaves another plausible prose path through the arbitrary gap and
   repeats the audit failure mode.
3. **Remove deterministic proposal reconciliation — rejected.** This would make
   the production result model-dependent and would discard established compact
   OpEx, monthly, and combined-component safeguards.

## Design

### Evidence precedence

`_COMBINED_RENT_OPEX_RE` remains highest specificity for component expressions
such as `$14 + $4 OpEx` and `$1.25 NNN + $0.34 OPEX`. Its OpEx component is
returned before any total rejection or standalone matching.

All other extraction uses two kinds of evidence:

- **Rejected combined totals.** Two bounded, symmetric patterns require an
  OpEx/CAM label, a rent label, an additive or inclusion relation (`plus`, `and`,
  `on top of`, or `in addition to`), a totalizing predicate (`is`, `equals`,
  `totals`, `comes to`, or `amounts to`, with optional `combined`, `all-in`, or
  `gross` wording), and one dollar figure. The detector returns the exact numeric
  span and its annualized `Decimal` value.
- **Valid standalone candidates.** Two narrow positive patterns cover only the
  audited production component-list form (`CAM, taxes, and insurance are running
  roughly $3.90`) and a structurally parenthetical rent modifier (`CAM, on top of
  base rent, is $3.90` or `CAM (in addition to base rent) is $3.90`). Ordinary
  compact forms remain owned by `_OPS_EX_RE`.

The arbitrary 96-character matcher and its rent-relation exceptions are removed.
Bare `NNN` remains excluded from the narrow positive patterns because it commonly
describes the rent basis rather than a separate operating-expense component.

### Extraction safety

The standalone-candidate collector evaluates narrow positives before legacy
candidates so the exact production `3.90` outranks the earlier `$14.10 NNN`
lease-basis match. It applies the existing hypothetical and monthly guards and
rejects any candidate whose numeric span overlaps combined-total evidence.
Legacy fallback uses the same rejected spans, so a rent-first total such as
`Base rent plus CAM equals $18` cannot be re-admitted by `_OPS_EX_RE`.

Multiple sentences remain independent: in
`CAM plus base rent totals $18/SF. CAM alone is $3.90/SF.`, only the first numeric
span is rejected and the later standalone `3.90` is returned.

### Proposal-write safety

Before the event-specific early return and before `_fill`, proposal augmentation
resolves the configured OpEx column and compares an existing model value with the
normalized rejected combined-total values from the fresh inbound text. It removes
only an exact matching model OpEx update, and only when the same normalized value
is not also supported by a valid standalone OpEx candidate. No other model update
or event is altered.

This ordering prevents terminal or new-property proposals from carrying a known
combined total into the Sheet, while allowing `_fill` to add a later valid
standalone value after a rejected model total is removed.

## Acceptance criteria

- The exact Wave B-M1 sentence extracts `3.90` and overwrites model OpEx `14.10`.
- Each audited rent-and-CAM total, including rent-first order, extracts no OpEx.
- A preseeded model OpEx equal to a rejected total is removed before an event
  early return.
- A later standalone `3.90` wins over an earlier rejected combined total in both
  direct extraction and proposal augmentation.
- Pending, unknown, prior, and unresolved projected-range language does not
  fabricate an OpEx value; a current standalone value remains eligible.
- Parenthetical rent modifiers, distinct rent and CAM figures, combined component
  expressions, and monthly annualization remain green.
- The focused five-file regression suite, syntax compilation, diff checks, and
  clean-worktree check pass.

## Scope and release boundary

Only `email_automation/ai_processing.py`, its focused regression tests, and these
planning artifacts change. Recipient, outbox, access, notification, scheduler,
and deployment code are out of scope. This correction is committed and reviewed
locally; it is not pushed or deployed in this task.
