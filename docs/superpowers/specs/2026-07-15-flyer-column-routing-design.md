# Flyer and Floorplan Column Routing

## Context

The final Baylor browser campaign reproduced a routing defect that Jill had reported. The uploaded sheet contained an existing `Flyers` column, but column analysis stored it as a custom `note` field rather than the canonical `flyer_link` field. The AI then wrote attachment prose into `Flyers`. When the attachment pipeline later uploaded the actual flyer, it correctly avoided overwriting the occupied cell and created `Flyers 2`.

The Drive upload and one-link-per-cell writer behaved as designed. The defect is inconsistent recognition of the plural `Flyers` header at the dashboard and worker boundaries.

## Required Behavior

1. `Flyer`, `Flyers`, and `Flyer / Link` are canonical flyer-link headers.
2. `Floorplan`, `Floor Plan`, and `Floor Plans` are canonical floorplan headers.
3. Asset columns are reserved for asset URLs. AI-generated prose must never be written into them, including when a persisted legacy campaign config marks the column as a custom note.
4. The first flyer URL uses the existing flyer-family column. The first floorplan URL uses the existing floorplan-family column.
5. Additional distinct files of the same type use numbered columns in that family, such as `Flyers 2` or `Floorplan 2`, with one URL per cell.
6. Flyer and floorplan attachments must never be routed into an invented `Property Image` column.
7. Existing user-entered non-link values are not overwritten. Numbered overflow columns remain the non-destructive fallback when a legitimate value occupies the base asset cell.

## Design

### Dashboard Boundary

Add the normalized header alias `flyers` to the dashboard's deterministic `flyer_link` alias table. Deterministic analysis will classify the physical `Flyers` header as `flyer_link` with `track` mode, overriding conflicting or missing AI analysis. Newly uploaded campaigns will persist a canonical mapping instead of a custom note field.

### Worker Boundary

Add `flyers` to the worker's canonical `flyer_link` aliases. The worker's existing asset-column guard will then reserve `Flyers` for the attachment pipeline even if an older persisted campaign config still contains `customFields.Flyers.mode = note` and no canonical flyer mapping.

The attachment writer itself will not be redesigned. It already separates flyer and floorplan families, writes one URL per cell, deduplicates retries, preserves occupied cells, and creates numbered same-family columns only for additional files or non-destructive overflow.

## Data Flow

1. Dashboard reads sheet headers.
2. Deterministic aliases classify `Flyers` as `flyer_link` and `Floorplan` as `floorplan`.
3. Campaign configuration stores canonical mappings and track-only asset modes.
4. Worker receives a broker reply and builds proposed sheet updates.
5. Asset-column guard rejects any AI text proposal targeting flyer or floorplan columns.
6. Attachment pipeline uploads each file and writes its Drive URL into the first available cell in the matching asset family.
7. A second distinct same-type asset receives a numbered same-family column; a different asset type receives its own base family column.

## Error Handling and Compatibility

- Legacy campaign configs are protected by canonical header recognition in the worker, independent of persisted mappings.
- Existing cell values are preserved; the fix does not infer that arbitrary text is safe to replace.
- Existing duplicate-link and partial-write retry behavior remains unchanged.
- Singular and established canonical headers retain their current behavior.

## Verification

Dashboard regressions will prove that `Flyers` is deterministically classified as `flyer_link`, receives `track` mode, and cannot be left as an unmapped custom note because of conflicting AI output.

Worker regressions will prove that:

- `Flyers` is recognized as an asset column under a legacy misclassified config.
- AI prose targeting `Flyers` is skipped as `handled-by-asset-pipeline`.
- One flyer URL writes into the existing `Flyers` cell without creating `Flyers 2`.
- Existing multiple-file behavior still writes one clickable URL per same-family cell.
- Flyer and floorplan URLs continue to land in separate families and do not create `Property Image`.

After automated verification and reviewed deployment, a fresh Baylor-owned browser campaign will repeat the failing attachment scenario. Jill's live campaign remains read-only during development and deployment.

## Release Gate

The fix clears this blocker only when a fresh browser campaign shows the existing `Flyers` cell holding the single flyer URL, the existing `Floorplan` cell holding the floorplan URL, no unexpected `Flyers 2` or `Property Image` column, and clean worker/queue state after completion.
