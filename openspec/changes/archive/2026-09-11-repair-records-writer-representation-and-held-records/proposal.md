# Repair the Records writer: strategy-aware representability, field-addressed refusals, held records

## Why

A canonical Records collection whose contract is "preserve the exact published text" cannot be written through the Records writer today: representability is checked strategy-blind, so any value containing a newline is refused as `UNREPRESENTABLE_RECORD_VALUE` even for Markdown-item frontmatter, which the writer's own quoting already round-trips losslessly. The refusal names no field, `item_key` reads as the natural key it is not, and a refused append discards the candidate, so the ledger ends the episode silently incomplete while the agent loops on Evidence. Live dogfood on 2026-09-09 hit all four (KB: `Notes/Failures/public-posts-v2-record-append-natural-key-and-unrepresentable-value-diagnostics-failure`); the one committed multi-line item carries a pre-escaped value that no longer hashes to its declared digest.

## What Changes

- Representability becomes a property of the storage strategy. Markdown-item and dataset values MAY contain newlines; the writer proves losslessness by serialising the frontmatter and parsing it back before commit. Markdown-log values keep refusing newlines because headings, notes and delimited child rows cannot carry them.
- Item validation refusals (`UNREPRESENTABLE_RECORD_VALUE`, `SCHEMA_UNKNOWN_FIELD`, schema type and enum failures) name every failing field path with its reason and received value class in one response, following the shipped "refusals name offending arguments" convention.
- Supplying a natural-key value as `item_key` returns remediation that names the collection's declared natural key, states that `item_key` is the internal UUID, and says to omit it.
- **Held records.** A refused Records append or update holds the complete candidate as a human-owned file under the collection with the diagnostics attached, returns the `held` reference beside the refusal, and lets the caller resume it by reference (`append`/`update` with `held=`), optionally overriding fields, or discard it. Held files are not items: they are not counted, queried, recalled or hashed into the audit head. Holding is the default and can be declined per call. Holding itself soft-fails: if the hold cannot be written, the original refusal is returned unchanged with a warning, never a second error.
- Collection inspection and the Records inventory report `coverage` with committed and held counts and the held references, so a blocked ledger is visible without reading files.
- The shipped skill scaffold and plugin copy gain one sentence: a refused Record write is held, the response names the field, fix and resume by reference; never loop on Evidence.
- Not in scope: presentation changes, schema-evolution suggestions from held items, due-state carriers for held counts, Planning-surface hold arguments, and any vault-content repair (the live Public Posts items are repaired operationally through `record_memory` after deploy).

No model or network call is introduced; every new behaviour is deterministic substrate logic on the existing guarded mutation path.

## Capabilities

### New Capabilities

None. Held records are a Records-surface behaviour over the existing structured-collection mutation contract, so they are specified inside the existing capabilities rather than as a new spec directory.

### Modified Capabilities

- `records`: ADD "Representability is a property of the storage strategy"; ADD "Refused Record writes are held, inspectable and resumable"; MODIFY "Records authoring is self-describing and safely preflightable" (field-addressed refusals; `describe` explains `item_key` versus the natural key); MODIFY "Records inventory is available before a selector is known" (per-collection coverage counts).
- `structured-collections`: ADD "Item validation refusals are field-addressed and complete"; MODIFY "Collection-scoped item identity and exact source versioning" (natural-key value supplied as an item key refuses with remediation); MODIFY "Manual-edit visibility and report-only inspection" (inspection reports held candidates and coverage without adopting them).

## Impact

- Code: `src/exomem/records.py` (validation, append/update, hold/resume/discard), `src/exomem/record_memory.py` (argument matrix, docstring), `src/exomem/record_governance.py` (inspection and inventory coverage), `src/exomem/vault.py` (frontmatter round-trip helper if not already pairable), `src/exomem/_scaffold/_Schema/references/planning-records.md` and `mutation-results.md` plus the md5-identical plugin copy and hosted skill renders.
- Compatibility: additive. Existing items and manifests are untouched; error codes keep their names and gain `details`; new optional arguments only. A new human-owned file type `type: held-record` appears under `Records/<Collection>/Held/` only after a refusal. Recall, query, adapter reads and the audit chain must ignore that directory, and tests prove it.
- Tool surface: `record_memory` argument and description changes move the tool-surface fingerprint and may touch the compact bootstrap byte budget; both are re-pinned deliberately with the measured sizes recorded in tasks.
- Downstream: the follow-on change (collection claims, coverage family, promotion sensor) consumes the `coverage.held` count as its "blocked" state; nothing here depends on it.
