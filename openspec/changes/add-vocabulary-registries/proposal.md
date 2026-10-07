## Why

Exomem's category sets live in four places with four contracts. Entity types and relations can be extended by a vault, but a save keeps no history and cannot be reverted. Source kinds and the core entity types are Python constants. Semantic categories have their own loader. An agent that promotes a wrong category leaves the owner with no undo, and the bootstrap lists entity types without saying which ones the vault actually uses.

The owner ruled on 2026-10-07 that every category set is a registry with a shipped default pack, that agents promote and humans revert, and that the owner's vault has no approval gate. This change builds the shared substrate those rulings need and moves the existing registries onto it. Later registries (statuses, note types, language packs, Planning values) land on the same substrate in their own changes.

## What Changes

- Add one generic vocabulary substrate: a registry spec per registry, one loader, shipped packs under `src/exomem/vocabulary/packs/core/`, an effective digest per snapshot and a per-vault cache keyed on file stat and then content digest.
- Move the core entity types and source kinds from Python into pack YAML, and move `core-relations.yaml` into the pack directory. The effective values do not change.
- Read the existing vault overlays (`entity-types.yaml`, `relation-registry.yaml`, `source-taxonomy.yaml`, `semantic-language-registry.yaml`) through per-registry adapters. No vault file moves or is rewritten until its next governed save.
- Give entity types, relations, the source taxonomy and semantic categories history and restore. Every save, including the old save operations, commits the overlay, a snapshot of the replaced bytes and a `log.md` entry as one batch.
- Give `schema_memory` one contract for every registry: `inspect` (paginated, with usage counts), `propose` (read-only), `save` (a hash-guarded delta with a reason), `history` and `restore`. The old operation names keep working.
- Apply the owner's governance rule: an owner save takes effect at once; a save by a restricted principal under a governed policy becomes a pending item in `review_memory(mode="vocabulary")`; usage counts and save reasons are owner-only aggregates.
- Replace the bootstrap `entities` section with a `vocabulary` section: the top 12 keys per registry by usage count, the findings and the `inspect` route. Counts come from maintained projections and read "unavailable" when a projection is cold, never 0. The core carries a pointer instead of an entity-type list.
- Add a repository rule that the registries are the only home for vocabulary and that code branches only on closed attribute values.

## Capabilities

### New Capabilities

None. The substrate is specified through the capabilities it changes.

### Modified Capabilities

- `entity-type-registry`: core types come from a shipped pack; saves keep history and can be restored; the delta contract and its meaning rules.
- `relation-vocabulary-evolution`: the core relations come from a shipped pack; saves keep history and can be restored.
- `agent-bootstrap-contract`: the `vocabulary` section and its count honesty; the core pointer.
- `command-surface`: the generic `schema_memory` registry contract, its governance and its published-surface rule.

## Impact

- Code: a new `src/exomem/vocabulary/` package; `entity_types.py`, `relation_registry.py`, `source_taxonomy.py`, `semantic_language_registry.py` become adapters over it; `registry_history.py`, `commands.py` (`op_schema_memory`, bootstrap), `bootstrap_core.py`, `vocabulary_review.py`, `vocabulary_auxiliaries.py`, `file_watcher.py`, `lexstore.py` and one selector row in `governance/egress.py`.
- Published surface: the `schema_memory` tool description changes once. No parameter is added. Generated plugin, hosted-candidate and capability artifacts are regenerated.
- Vaults: no file is rewritten by the upgrade. A restore can leave pages that use a removed key; they are reported as unregistered debt and their bytes are never touched.
- This change amends `activate-agent-led-vocabulary-evolution`: v1 saves take effect immediately and revert belongs to this change; its work-item queue is the nudge channel.
- Related: `shrink-bootstrap` names the `entities` section and the core entity-type list; its requirement is amended to the `vocabulary` section and the pointer.
