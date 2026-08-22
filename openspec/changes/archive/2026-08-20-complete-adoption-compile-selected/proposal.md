# complete-adoption-compile-selected

## Why

Existing-vault adoption currently stops at safe scan, manifest save, and source copying while `compile-selected` is only advertised as planned. The next adoption step should let a user select messy legacy notes, preserve originals, copy source material with provenance, and receive a deliberate compilation plan without silently migrating or rewriting anything.

## What Changes

- Implement `adopt(mode="compile-selected")` as a non-destructive compile-planning mode.
- Reuse existing safe selection/copy behavior for legacy text files outside `Knowledge Base/`.
- Return reviewable compilation proposals backed by governed source paths, not auto-written compiled notes.
- Add stable adoption context refs for originals, copied sources, manifests, and compilation proposals.
- Update setup and product docs so one-command onboarding points to manifest review, copy-as-sources, and compile-selected planning.

## Capabilities

### New Capabilities
- `adoption-compile-planning`: selected legacy material can be copied into governed Sources and turned into read-only compilation plans with stable refs.

### Modified Capabilities

(none)

## Impact

- `src/exomem/adopt.py`, `src/exomem/setup_wizard.py`, and a small context reference helper.
- `adopt` public behavior across MCP, CLI, and REST because it gains an implemented mode and updated docs/schema text.
- Adoption tests, setup wizard tests, context-ref tests, MCP schema fixture if the tool description changes, and onboarding/product docs.
