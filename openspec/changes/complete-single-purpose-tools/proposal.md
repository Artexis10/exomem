## Why

The Records and Planning pilot exposes exact operation inputs and separates reads from writes. Other grouped tools still mix effects and unrelated optional arguments. Extend the proven rendering boundary across those families while preserving their execution contracts.

## What Changes

- Expose remaining grouped operations through typed read and write tools, with one coherent object per tool.
- Keep the grouped commands as hidden compatibility spellings and stable execution identities on MCP. Preserve REST and CLI contracts.
- Represent conditional effects and credential distinctions in operation entries. Generate schemas, normalization and coverage checks from those entries.
- Update MCP guidance, generated artifacts and hook classifications together.
- Clarify collection selectors and type query filter items from the existing query contract.
- Measure the new surface against the grouped baseline with harder, source-derived workflows. Remove the temporary baseline option after the decision run.

## Capabilities

### Modified Capabilities

- `command-surface`: Complete typed operation renderings, honest effect annotations, compatibility normalization and active-surface guidance across the remaining grouped families.

## Impact

The change extends `operation_entries.py`, `operation_schema.py`, the command registry and MCP registration. Authorization, leases, receipts and leaves retain the normalized grouped call. Generated schemas, plugin packages, capability docs and skill references change together. The existing tool-selection harness supplies paired measurements. Optional media and Cloud capabilities retain their current admission rules and exclusions.

Collection-adoption renderings follow the containing collection-adoption merge. Cloud rollout and directory rescan remain separate operational steps. This change does not alter `recall_context` inputs or enable excluded Cloud operations.
