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

`content_hash` preserves the legacy hash of UTF-8 overlay text with universal-newline normalization, or `none` without an overlay. It is the public `expected_hash` checked by a save, so a hash an old client read still works. `effective_digest` hashes the canonical effective entries, with pack and overlay merged. Two overlays that resolve to the same entries share it. Consumers use that digest to detect vocabulary changes.

The snapshot retains the exact UTF-8 decoded overlay text for history and publication. The canonical writer guards its raw bytes independently of the public text hash. Roles and conventions keep their existing effective audit hashes.

### 5. Cache keyed on stat, then exact text

The loader caches one snapshot per vault and registry. An unchanged `(mtime_ns, ctime_ns, size, inode)` returns the cached snapshot without a read. A changed stat reads the exact text and reuses the snapshot only when that text is unchanged. A newline-only edit refreshes the raw preimage even when the public hash and effective entries stay unchanged. A governed save drops the entry, as does a `_Schema/*.yaml` watcher event.

### 6. Saves are deltas with meaning rules

`save` takes `{"upsert": {key: entry}, "alias": {key: [aliases]}, "deprecate": {key: replacement}}`, the `expected_hash` and a `why`. The adapter writes each generic entry into the overlay grammar. On an existing entry the parent and behaviour attributes are immutable, aliases are only added, and a deprecated entry stays deprecated with the same immediate replacement. The registry parser then validates the whole result, so collisions, invalid parents and broken chains refuse the save before anything is written. An observed key is never deleted, only deprecated.

Every save and every restore is one batch through `registry_history.commit`: the overlay, a snapshot of the replaced bytes and a `log.md` entry. The old operations (`save-entity-types`, `save-relations`, category `infer save=true`) commit the same way.

Restore writes the exact bytes of a kept version. It skips the continuity and observed-key checks, because reverting is its purpose, and it reports the keys the restore removed. Pages that still use a removed key keep their bytes and surface as unregistered debt through the existing audit findings.

Under v2 additive authority the batch writer classifies every staged write. The snapshot and the log entry are sealed as derived auxiliaries of the registry write (`registry-history` and `operation-log`), so classification still sees exactly one registry change.

### 7. Separate mutation authority from disclosure admission

Registry writes use the existing bound-principal ownership and canonical vocabulary writer authority. Reuse the existing owner predicate from the principal boundary. Transport and authorization-session boundaries retain authentication and live validation; registry code creates no second authority engine.

In v1, a valid owner's save takes effect immediately. A content restriction alone never creates an approval item for that owner. A resolved nonowner's save records the existing pending work item in `review_memory(mode="vocabulary")`, carrying the reason and delta. The registry stays unchanged; a registry without a review family refuses with `audience_restricted`. An unresolved caller cannot become an owner or a fabricated delegate through absent context.

Restore requires owner write authority independently of disclosure permission. A trusted internal invocation establishes `library_scope()` at its existing entry point. That scope preserves any already-bound remote principal. The legacy hosted RAW exemption supplies no owner write authority.

Explicitly activated v2 vaults retain the canonical writer's effect classification, grants and approvals. S1 never activates that mode or substitutes a queue for its writer gate. The existing opt-in owner-control work remains T13; S1 does not make its approval process mandatory for v1 owners.

`egress.owner_only_aggregate` remains a disclosure decision for whole-vault counts, reasons and other private-dependent results. Reuse current RAW admission, including protection without configured file policy. This decision neither grants nor removes write authority.

An operation that requires unavailable private registry information reports unavailable. Counts-only filtering does not protect global keys, collisions, aliases, folders or hashes. Each adapter admits its declared overlay before it reads private definitions. History admits each snapshot before reading its bytes.

A pending proposal requires admitted inputs and a server-verified target hash. The queue binds that target alone when validation reads no other overlay. Complete limited-owner promotion across private domains follows the vault-consolidation domain delivery. A legitimate write whose outcome is independent of private definitions remains available.

What this prevents: a restricted principal reshaping vocabulary for everyone, for example hijacking resolution with an alias. When it fires wrongly, the delegate's label waits for the owner's next session; the delegate pays, and its page write still lands with the raw label.

The pending item targets the registry overlay as a virtual review target that only the authorized owner can see. Its version is the overlay hash, so an unrelated save makes the item stale and the owner refreshes it.

### 8. Counts come from maintained projections

Usage counts are read from projections that the indexer already maintains: the graph snapshot for entity pages, authored relations and semantic units by category, and the lexical catalogue for source kinds and domains. A projection that is absent, warming, stale or refused makes that registry's counts `unavailable` with a reason. A zero is only ever a counted zero. Counts are never computed by scanning Markdown on a read path.

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
