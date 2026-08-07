# Explicit OpEx Precedence Hotfix

Date: 2026-08-07

Status: approved within the active SiteSift production-readiness build

## Production finding

The Wave B-M1 broker reply stated an asking rent of `$14.10 NNN` and later stated `CAM, taxes, and insurance ... $3.90 per square foot`. The deterministic OpEx fallback returned `14.10`, causing the Sheet to duplicate rent into `Ops Ex / SF` and calculate monthly gross rent as `$44,062.50` instead of `$28,125.00`.

The failure is deterministic: `_OPS_EX_RE` finds only the earlier figure-first `$14.10 NNN` match because the later keyword-first `CAM` clause exceeds the regex's short linking-clause grammar. `_opex_match_is_rent_basis_line` also does not reject the first match because the nearby phrase is `we can offer ... at`, not one of its rent-keyword forms.

## Considered approaches

1. Rank explicit keyword-first OpEx evidence before ambiguous figure-first `NNN` evidence. This is the selected approach because `CAM`, `OpEx`, `TMI`, and `operating expenses` explicitly name the field while bare `NNN` commonly describes rent basis.
2. Expand the rent-line guard to recognize `offer ... at $X NNN`. This fixes the observed sentence but leaves the parser dependent on an incomplete list of rent phrasings.
3. Stop deterministically correcting model-proposed OpEx. This would make the production result model-dependent and remove a safety layer that already catches known extraction errors.

## Design

Add a narrow explicit keyword-first candidate extractor that accepts ordinary prose between an unambiguous OpEx label and a nearby dollar figure, including commas and phrases such as `taxes, and insurance are running roughly`. It must stop at sentence, newline, semicolon, or intervening-dollar boundaries and must not treat bare `NNN` as an explicit keyword-first label.

The rent-reference guard must reject only a rent phrase that assigns the captured dollar figure, such as `asking rent is $14.10`. It must not reject relational language in a genuine OpEx clause, such as `CAM, on top of the base rent, is $3.90`.

`_extract_ops_ex_sf_from_text` will preserve the existing combined base-plus-OpEx matcher as the highest-specificity path, then evaluate this explicit candidate before general `_OPS_EX_RE` candidates. Existing monthly-basis normalization and hypothetical-language rejection remain in force. The existing general matcher remains the fallback for compact forms such as `$8/SF opex` and `NNN charges are $7.25/SF/yr`.

No notification, recipient, outbox, access-control, replacement, or deployment behavior changes in this hotfix.

## Acceptance criteria

- The exact Wave B-M1 production sentence returns rent `14.10` and OpEx `3.90`.
- The proposal augmentation replaces a conflicting model OpEx value of `14.10` with `3.90` before the Sheet write.
- Explicit OpEx figures continue to win over earlier rent-basis `NNN` figures.
- NNN-only rent statements do not fabricate OpEx.
- `CAM pending; quoted rate $14.10 NNN` does not cross the semicolon and fabricate OpEx.
- `CAM, on top of base rent, is $3.90` retains the explicit OpEx value.
- Hypothetical and monthly-basis safeguards remain green.
- Focused extraction tests, Jill live-regression tests, the backend safety suite, deploy contracts, syntax checks, and diff checks pass before release.

## Production verification

Deploy only the reviewed exact commit to a no-traffic Cloud Run revision, verify immutable image/config/health, promote to the Baylor-only lane, and run a new browser-driven internal campaign using fresh wording that contains a rent-basis figure followed by an explicit OpEx/CAM figure. The live Sheet must contain the correct distinct values and gross formula result.
