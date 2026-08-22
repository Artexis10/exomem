## Context

The released product already has four pieces that should be composed rather than replaced: deterministic audit queues, RRF-ranked `attention`, a derived epistemic graph, and block-level relation metadata such as `- relations: supports: [[A]]`. The graph index also recognizes typed list lines, but that syntax has no canonical authoring contract, shared parser, write feedback, or vault-wide repair loop. As a result, agents often emit untyped prose or no wikilinks at all, leaving valid compiled notes weakly connected in Obsidian.

The existing attention report has the opposite problem: useful signals are recomputed correctly, but have no stable review identity or durable user decision. A dismissed item therefore returns forever, while a client cannot address one item independently of its current rank.

## Goals / Non-Goals

**Goals:**

- Establish an Exomem-native, Markdown-visible relation contract without replacing semantic blocks or importing another product's ontology.
- Surface relation debt through the same deterministic review queue as contradictions, stale conclusions, and source backlog.
- Give review items stable references and fingerprint-bound dismiss/snooze/reopen state.
- Keep `review_memory` read-only and put all state changes behind a separate explicit command.
- Make the blessed `exomem review` command useful to a person while preserving stable JSON output.

**Non-Goals:**

- No automatic semantic judgment, relation acceptance, bulk vault rewrite, or server-side reasoning model.
- No visual graph UI, generalized `exomem://context` resolver, visual Evolution view, or full Adoption Studio in this change.
- No requirement that every note have a relation; a relation-debt finding is a review proposal, not a write rejection.
- No replacement of existing inline wikilinks, frontmatter provenance, supersession links, or block-level relations.

## Decisions

### Relation syntax has three explicit layers

Canonical note-level edges live in a `## Relations` section as one directional edge per bullet:

```markdown
## Relations
- refines [[Earlier Conclusion]]
- depends_on [[Architecture Decision]]
```

Relation names are lower `snake_case` and use Exomem's existing governed relation vocabulary. Inline wikilinks remain generic `links_to`; semantic-block metadata remains the precise way to relate a claim, finding, decision, or piece of evidence. This gives Obsidian visible links, readable direction and meaning, and block-level precision without copying Basic Memory's entity/observation ontology.

The current relation-line recognition in `epistemic_graph.py` will move behind one shared parser used by graph indexing, semantic-block validation, audit, and write feedback. Existing recognized relation lines outside `## Relations` remain index-compatible, but new authoring and relation-debt checks recognize the canonical section. A canonical typed edge suppresses a redundant `links_to` edge for the same relation line inside the epistemic graph; ordinary Obsidian backlink behavior is unchanged.

Alternatives rejected: free-form `## Connections` prose cannot be queried consistently; accepting arbitrary relation labels silently fragments the graph through spelling drift; putting all note-level edges in YAML hides them from normal reading and editing.

### Relation debt is measurement and repair remains proposal-first

`audit(category="relation_debt")` scans active, writable compiled pages and surfaces pages with no outbound body wikilinks or typed relations. It excludes append-only material, curated/read-only trees, archived/superseded pages, indexes, hubs, and snapshots using the same access and lifecycle rules as stale review. Findings carry a content-derived signal version and direct the caller to `connect_memory(operation="suggest-relations")` or `suggest-links`.

No embeddings or model are required for the audit. Optional model-backed relation suggestions remain explicit in `connect_memory`; the Inbox never calls them automatically.

### Review identity is stable while decisions bind to signal fingerprints

An attention item's identity is derived from the canonical `exomem://memory/<uuid>` target when available, with a deterministic path reference fallback for legacy pages. Its public reference is `exomem://review/<id>`. Identity is independent of queue rank and reason text.

Each response also carries a fingerprint over the item identity plus contributing categories, related target identities, and content-derived signal versions emitted by the audit checks. Dismiss and snooze records apply only while that fingerprint matches. If a note, source, contradiction partner, or set of reasons materially changes, the same stable review item automatically resurfaces with a new fingerprint.

Alternatives rejected: hashing rendered details would resurface stale items every day as age counters change; binding state only to the target would hide future, materially different issues forever.

### Portable state is transparent JSON, not a derived index

Review state lives at `Knowledge Base/.review-state.json` with a schema version and records keyed by review item ID. It is portable user state, not rebuildable measurement state, so SQLite is inappropriate. Writes use process locking plus atomic replacement. Reads soft-fail to an empty state when the file is absent; malformed state returns an explicit operation error rather than silently overwriting user decisions.

The state file stores only item ID, reviewed fingerprint, action, optional snooze date/reason, and update timestamp. It never stores note content. `dismiss`, `snooze`, and `reopen` are logged as explicit triage results but do not modify governed notes.

### Read and write surfaces remain permission-separable

`review_memory` remains read-only and gains a state filter (`open` default, `all`, `snoozed`, or `dismissed`) plus item lookup by review reference. A new `triage_memory` registry command owns `dismiss`, `snooze`, and `reopen`, and is exposed consistently over MCP, REST, and CLI.

The `exomem review` alias renders a compact inbox by default and retains `--json`. Nested human commands route to `triage_memory`; they do not duplicate state logic.

## Risks / Trade-offs

- [Risk] Relation debt initially produces a large queue on older vaults. -> Keep it lowest in attention tie preference, preserve category filters, and never mutate in bulk.
- [Risk] A fixed relation vocabulary cannot express every domain. -> Ship the existing broad epistemic vocabulary plus `relates_to`; defer user-owned vocabulary extensions to schema evolution rather than accepting typo-prone arbitrary labels now.
- [Risk] Path fallback review IDs change when a legacy page moves. -> Prefer canonical memory IDs and document ID backfill; preserve compatibility for untouched legacy vaults.
- [Risk] Concurrent triage writes race. -> Serialize in-process writes and atomically replace the small JSON file; Exomem supports one service owner per vault.
- [Risk] State filtering changes visible totals. -> Return both visible counts and state-summary counts while preserving existing fields when no state exists.

## Migration Plan

1. Land the shared relation parser and graph/write-feedback tests with no vault mutation.
2. Add relation-debt audit and attention enrichment; existing calls remain compatible and only gain fields/categories.
3. Add review state plus `triage_memory`; absent state files behave exactly like current attention.
4. Add human CLI rendering and update the scaffold/docs.
5. Use the Inbox to review legacy relation debt incrementally. Do not run an automatic vault rewrite.

Rollback removes the new command and ignores `.review-state.json`; existing Markdown relations remain ordinary Obsidian-compatible wikilinks, and all prior attention fields continue to work.

## Open Questions

None. Generalized context references, a visual Evolution view, and Adoption Studio are intentionally follow-on changes built on these IDs and relations.
