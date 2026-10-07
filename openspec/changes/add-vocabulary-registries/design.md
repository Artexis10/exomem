## Context

The owner ruled on 2026-10-07 that every category set is a registry with a shipped default pack, that an agent promotes a category at once when nothing fits, and that a human can revert or change it. The owner's vault has no approval gate. This design records the decisions for the shared substrate and for the first slice (S1), which moves the existing registries onto it. The slice plan for the whole programme is in `tasks.md`.

Today the relation registry already holds the meaning rules the substrate needs: a new entry names a parent, `family_mismatch` stops a declared family from relabelling its parent, meaning is never rewritten in place, aliases are only added, a deprecation chain ends in an active survivor, and saves are hash-guarded deltas. `registry_history` already gives `context_roles` and `activation_conventions` snapshots, a `log.md` entry and restore. Entity types, relations, the source taxonomy and semantic categories have none of that history.

## Goals / Non-Goals

**Goals:** one loader and one contract for every registry; history and restore for the four existing registries; honest usage counts in bootstrap; the owner's governance rule at one decision point; no change to what an unchanged vault resolves.

**Non-goals for S1:** skill text, lifecycle statuses, note types, language packs, nudges and Planning values. Each has its own slice. S1 also rewrites no page and adds no tool parameter.

## Decisions

### 1. One loader, one spec per registry

`src/exomem/vocabulary/registry.py` owns reading, caching, digests, delta application, history and restore. A `RegistrySpec` declares the shipped pack, the overlay path, the history stem, the key grammar, which generic fields and attributes a delta may carry, which of them are immutable in place, whether deprecation needs a replacement, the promotion mode, the cap and the review family. A per-registry adapter parses the pack and overlay into the module's typed registry and maps it to generic entries.

The existing modules keep their public APIs. `entity_types.load_entity_types`, `relation_registry.load_registry`, `source_taxonomy.load_taxonomy` and `semantic_language_registry.load_registry` read through the loader. Their parsers stay the validators, because each one already encodes rules a rewrite would have to reproduce exactly.

### 2. Packs reproduce today's values exactly

The core entity types, source kinds and domains move from Python into `packs/core/*.yaml`, and `core-relations.yaml` moves to `packs/core/relations.yaml`. A parity check compares each Python constant with its pack on an unchanged vault and on a vault with legacy overlays. It runs in this change and is deleted together with the constants it compares. Semantic categories keep their Python core in S1; their pack moves with the slice that makes their aliases the source for working-set categories.

### 3. Overlays keep their path and their grammar

No vault file moves, and the upgrade rewrites nothing. A governed save renders the overlay in that registry's own grammar through its adapter, so an older release that reads the file after a rollback still sees every entry. The generic entry grammar (`key`, `label`, `description`, `aliases`, `status`, `replaced_by`, `parent`, `attributes`, `guidance`, `provenance`) is the shape of `inspect`, `propose` and the `save` delta. A registry that has no legacy file, such as a later status or note-type registry, is written in that grammar from the start.

Per-entry provenance in S1 lives in history: each snapshot header records the operation, the reason, the principal hash and both hashes.

### 4. Two hashes with two jobs

`content_hash` is the hash of the overlay bytes, or `none` when there is no overlay. It is the `expected_hash` that guards a save, and it equals today's `extension_hash` and `content_hash`, so a hash an old client read still works. `effective_digest` is the hash of the canonical effective entries, pack and overlay merged. Two overlays that resolve to the same entries share it. Consumers that need to know whether the vocabulary changed key on it.

### 5. Cache keyed on stat, then digest

The loader caches one snapshot per vault and registry. An unchanged `(mtime_ns, size, inode)` returns the cached snapshot without a read. A changed stat reads the file and returns the cached snapshot when the digest still matches. A governed save drops the entry, and the file watcher drops it when it sees a `_Schema/*.yaml` event, which covers a same-second, same-size hand edit.

### 6. Saves are deltas with meaning rules

`save` takes `{"upsert": {key: entry}, "alias": {key: [aliases]}, "deprecate": {key: replacement}}`, the `expected_hash` and a `why`. The adapter writes each generic entry into the overlay grammar. On an existing entry the parent and behaviour attributes are immutable, aliases are only added, and a deprecated entry stays deprecated with the same immediate replacement. The registry parser then validates the whole result, so collisions, invalid parents and broken chains refuse the save before anything is written. An observed key is never deleted, only deprecated.

Every save and every restore is one batch through `registry_history.commit`: the overlay, a snapshot of the replaced bytes and a `log.md` entry. The old operations (`save-entity-types`, `save-relations`, category `infer save=true`) commit the same way.

Restore writes the exact bytes of a kept version. It skips the continuity and observed-key checks, because reverting is its purpose, and it reports the keys the restore removed. Pages that still use a removed key keep their bytes and surface as unregistered debt through the existing audit findings.

Under v2 additive authority the batch writer classifies every staged write. The snapshot and the log entry are sealed as derived auxiliaries of the registry write (`registry-history` and `operation-log`), so classification still sees exactly one registry change.

### 7. One governance decision point

`egress.owner_only_aggregate` decides. When it returns no refusal, a save takes effect at once. The shared decision checks verified owner authority, unbound internal calls and the current hosted exemption. Empty file policy alone does not make a bound nonowner unrestricted: RAW protects whole-vault metadata too. Registry code must reuse that decision without a second authority rule. Otherwise:

- `save` records a pending work item in the owner's `review_memory(mode="vocabulary")` queue, in the registry's review family, carrying the reason and the delta. The registry is unchanged. A registry without a review family refuses with `audience_restricted`.
- `restore` refuses with `audience_restricted`.
- `inspect` returns entries without counts, and `history` returns versions without reasons or principals.

What this prevents: a restricted principal reshaping vocabulary for everyone, for example hijacking resolution with an alias. When it fires wrongly, the delegate's label waits for the owner's next session; the delegate pays, and its page write still lands with the raw label.

The pending item targets the registry overlay as a virtual review target that only the owner can see. Its version is the overlay hash, so an unrelated save makes the item stale and the owner refreshes it.

### 8. Counts come from maintained projections

Usage counts are read from projections that the indexer already maintains: the graph snapshot for entity types and relations, and the lexical catalogue for source kinds, domains and semantic categories. A projection that is absent, warming, stale or refused makes that registry's counts `unavailable` with a reason. A zero is only ever a counted zero. Counts are never computed by scanning Markdown on a read path.

### 9. Bootstrap

The `vocabulary` section replaces `entities`. For each registry it carries the top 12 active keys by count with their counts, a `+n more` line, the finding count and the `inspect` route. The core carries a pointer to the section instead of entity-type ids. The section keeps the `vocabulary_workflow`, `relation_vocabulary`, `source_taxonomy` and `entity_registry` blocks; `entity_registry` no longer lists every type, because the section lists them by use. `section="entities"` is still accepted and serves the same blocks. Released hosted profiles keep their published payload.

### 10. The published surface changes once

The `schema_memory` description names the registry contract once. No parameter is added. A later registry is a new `subject` value in the same contract and needs no change to the tool surface or to `egress.py`. Live vocabulary never appears in a tool schema. `propose` is the one new selector, classified as `structure`.

## Risks / Trade-offs

- A restore removes a key that pages use → the pages keep their bytes and surface as debt; restore reports the removed keys.
- A stale same-size hand edit hides behind the stat key → the watcher invalidates `_Schema/` events and every governed save drops the entry.
- Counts cost a projection read on bootstrap → one grouped query per registry over an already-open snapshot, and `unavailable` on any doubt.
- The legacy grammar cannot hold per-entry provenance → provenance lives in the history header until a registry needs the generic grammar.

## Migration Plan

1. Ship the substrate with the packs and adapters. An unchanged vault resolves exactly as before.
2. The first governed save of a registry writes its first snapshot; until then `history` is empty.
3. Rollback: an older release reads every overlay as before, because S1 writes each overlay in its own grammar. Snapshots under `_Schema/history/` are ignored by older releases.
