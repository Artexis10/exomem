## Why

Planning and Records are structurally useful but are currently stored as implementation artefacts: UUID filenames, frontmatter-only items, weak relationship links, and presentation blocks whose lifecycle can drift from their collection recipe. That makes a governed vault correct for machines while remaining unpleasant to browse, search, and understand in ordinary Markdown tools such as Obsidian.

Dogfooding also exposed contract gaps rather than isolated bad data: Planning cannot revise or rebaseline a governed manifest, saved views can silently use vocabulary that no item can satisfy, and removing a presentation recipe can leave stale generated blocks behind without an inspection failure. These gaps should be fixed at the collection substrate so every surface produces the same readable, governed result.

## What Changes

- Add a shared contract for human-readable structured-item filenames and generated Markdown presentation across Planning and Records while retaining immutable IDs as canonical identity.
- Keep mutable workflow state out of filenames. Permit only stable natural-key fields in a filename recipe; Records may include an immutable event kind when it forms part of that natural key.
- Make renaming an explicit, guarded, audited operation. A title-bearing field change does not silently move a file, and inspection reports filename drift until the caller previews and applies the rename.
- Add a generic presentation recipe that renders meaningful item content and real Obsidian-compatible wikilinks from typed relationships on every successful mutation.
- Make presentation lifecycle safe: recipe removal must refuse while managed blocks exist or remove them transactionally, and inspection must report stale, orphaned, or unrenderable managed blocks.
- Give Planning governed manifest revision and rebaseline parity with Records, including validation of saved-view predicates against Planning vocabularies.
- Add a preview-first migration for existing UUID-named or frontmatter-only items, with collision reporting, exact planned moves, and all-or-nothing application.
- Preserve the existing Records presentation recipe as a supported compatibility form and provide an explicit migration to the shared recipe.
- Reuse the ordinary-note filename sanitisation rules defined by `add-readable-note-filenames`; this change owns structured collections and does not duplicate that feature's scope.

## Capabilities

### New Capabilities

- `human-owned-structured-files`: Defines stable human-readable filenames, readable generated item content, relationship wikilinks, explicit rename semantics, and preview-first migration for structured items.

### Modified Capabilities

- `structured-collections`: Adds shared naming and presentation recipes, transactional presentation removal, migration planning, and complete inspection of generated representation state.
- `planning`: Adds governed manifest revision/rebaseline, Planning presentation defaults, and vocabulary-aware validation of saved views.
- `records`: Defines compatibility between the Records-specific presentation recipe and the shared representation contract.
- `command-surface`: Exposes the same revision, representation-preview, and representation-apply behaviour through MCP, CLI, and REST.

## Impact

This changes collection manifest validation, structured-item create/update flows, path resolution, typed-link rendering, audit events, inspection, and maintenance actions. Existing collections remain readable and keep their current paths until an explicit migration is applied. Existing Records presentation recipes remain supported. The migration is potentially large but is bounded to one collection per approved apply operation and must be fully previewable before any file moves.
