## Why

Exomem already computes a strong read-only attention queue, but every invocation is a fresh JSON report: items have no addressable identity, users cannot snooze or dismiss a reviewed signal, and the blessed `exomem review` command is not shaped for daily human use. At the same time, Exomem's block-level relation grammar is not paired with a canonical whole-note Markdown relation format, so many compiled notes remain invisible or weakly connected in Obsidian despite semantic similarity. Productizing both concerns as an Epistemic Inbox creates the durable review and repair loop that later `exomem://` contexts and visual belief evolution can share.

## What Changes

- Give each attention item a deterministic ID, an `exomem://review/<id>` reference, a signal fingerprint, and canonical references for its target and related pages.
- Add a canonical `## Relations` Markdown grammar (`- relation_type [[Target]]`) for typed note-to-note edges while retaining Exomem's existing semantic-block relation metadata for block-level edges and generic inline wikilinks as `links_to`.
- Add a deterministic `relation_debt` review signal for active compiled notes with no explicit outbound connections or typed relations; semantic suggestions remain proposals until accepted.
- Persist portable review decisions in the vault: snooze, dismiss, and reopen. A decision applies only to the fingerprint that was reviewed, so materially changed evidence resurfaces automatically.
- Add a write-explicit `triage_memory` product command across MCP, REST, and CLI while keeping `review_memory` read-only.
- Make `exomem review` render a compact human inbox by default, retain `--json` automation, and expose `snooze`, `dismiss`, and `reopen` subcommands.
- Document the review lifecycle and its relationship to future generalized contexts, visual Evolution, and Adoption Studio work.

## Capabilities

### New Capabilities
- `epistemic-inbox`: Stable review-item identity, portable review state, explicit triage operations, and the human daily-review command.
- `markdown-relations`: Canonical, directional, Markdown-visible note-level relations with deterministic parsing and graph indexing.

### Modified Capabilities
- `attention-queue`: Add relation debt, enrich ranked attention items with stable references, and filter active, snoozed, and dismissed signals without changing the underlying deterministic ranking.

## Impact

- Adds small pure-Python relation and review-state modules plus a portable hidden JSON review-state file under the governed Knowledge Base.
- Extends `attention`, `review_memory`, the command registry, generated MCP/REST/CLI schemas, and capability documentation.
- Adds one explicit write-capable product command; existing `review_memory` calls and path fields remain compatible.
- No new dependency, background process, model, index, or steady-state resource cost.
