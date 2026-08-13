# CE-Q1 evidence boundary

CE-Q1A produces offline deterministic evidence from synthetic inputs. Its
`baseline-report.*` does not certify production, a model, a mailbox, delivery,
Google Sheets persistence, or cross-store atomicity.

All mutable runtime, wheel, process, and quarantine state stays below the
ignored `.ceq1-runtime/` and `.ceq1-venv/` roots and is never committed.
Committed evidence contains only scanned, synthetic, path-free records.

Any change to a relevant owner module, transitive runtime projection, fixture,
oracle, prompt/configuration, dependency lock, toolchain manifest, or sandbox
policy invalidates the affected report and requires a complete clean rerun.
