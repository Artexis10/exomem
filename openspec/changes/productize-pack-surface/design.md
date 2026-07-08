# productize-pack-surface - design

## Context

The previous cognition-layer tranche added built-in pack files and adoption
suggestions. This change promotes packs from "likely routing hints" to durable,
visible product primitives while keeping the typed write layer intact.

## Decisions

1. **Selection is metadata, not migration.**
   Selecting a pack writes a small manifest under `Knowledge Base/_Packs/`.
   It does not create folders, rewrite old notes, or auto-compile content.

2. **Built-ins define the public pack schema.**
   Built-in JSON files remain strict and generic. Unknown fields still fail
   validation so deployments cannot assume ignored metadata is active.

3. **Fresh vaults get explicit defaults.**
   When setup has no useful structural signals, it selects `personal-records`
   in non-interactive mode and lets interactive users choose one or more packs.

4. **Existing vaults combine inference with choice.**
   Adoption still suggests packs from deterministic structure. Setup persists
   accepted or adjusted selections so bootstrap can expose the user's active
   product surface.

5. **Agents route through typed tools.**
   Bootstrap and command metadata describe simple product actions plus pack
   guidance. They do not add server-side reasoning or bypass `add`, `note`,
   `preserve`, `find`, `get`, `audit`, `edit`, `replace`, and `link`.

## Data Shape

`Knowledge Base/_Packs/selected-packs.json`:

```json
{
  "schema_version": 1,
  "selected_pack_ids": ["personal-records"],
  "source": "setup",
  "updated": "2026-07-07",
  "packs": [{ "id": "personal-records", "name": "Personal records" }]
}
```

The manifest is rebuilt from current built-in metadata on write. Read helpers
tolerate a missing manifest and invalid selected IDs by falling back to the
default pack.

## Risks

- Pack metadata could become a second ontology. Mitigation: metadata maps to
  existing durable primitives and simple front-door actions only.
- Setup could imply migration. Mitigation: every output says selection is
  guidance and writes only under `Knowledge Base/`.
- MCP schema churn is easy to miss. Mitigation: update the committed schema
  fixture only when descriptions or input schemas change intentionally.
