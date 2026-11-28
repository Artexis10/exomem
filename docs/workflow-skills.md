<!-- authority:non-specification -->

# Workflow skills

Workflow skills are named agent workflows that sit above Evermatter's typed tools.
They are small `SKILL.md` files that tell an agent when to activate, which
Evermatter capabilities to use, what invariants to preserve, and what output shape
to return.

## How the layers differ

| Layer | What it is | Example |
| --- | --- | --- |
| Evermatter product commands | Product MCP/REST/CLI commands that read and write the KB | `search_memory`, `read_page`, `create_note`, `save_source`, `list_review_items` |
| Context packs | Retrieval-time evidence bundles returned by `search_memory(deep=true)` | top hits, extracted claims, graph neighborhood, contradiction signals |
| Knowledge packs | Product/domain guidance selected during setup | technical, creative, legal/warranty, personal records |
| Workflow skills | Agent-visible workflows for common user intent | `evermatter-continue`, `evermatter-capture`, `evermatter-review` |

Packs guide what domain a user is working in. Workflow skills guide what job the
agent is doing right now.

## Built-in workflow skills

- `evermatter-continue` - resume prior project or session context.
- `evermatter-capture` - save a durable conclusion without dumping transcripts.
- `evermatter-ingest` - preserve an external artifact under the `source_kind` and
  `domain` it actually belongs to, then compile what matters.
- `evermatter-research` - gather sources and save attributed findings.
- `evermatter-reflect` - extract decisions, failures, patterns, open questions, and next actions.
- `evermatter-curate` - improve links and compiled-note quality safely.
- `evermatter-defrag` - reconcile duplicate, stale, or conflicting memory.
- `evermatter-review` - drain review and audit queues.
- `evermatter-media` - search and inspect PDFs, images, audio, video, and other artifacts.

## Installation and discovery

The canonical definitions ship in:

```text
src/exomem/_scaffold/_Schema/workflow-skills/
```

`evermatter init` copies them into new vaults under
`.exomem/schema/workflow-skills/`, outside the note namespace.

`evermatter install-skill` installs the core `evermatter` skill and also installs each
workflow skill as a sibling Claude Code skill folder, so clients that support
skills can discover them directly.

Generic MCP clients that do not support skills should call `read_instructions()` once.
The bootstrap payload includes a compact `workflow_skills` index with names,
purposes, trigger examples, and the canonical vault-relative path.

## Invariants

Every workflow skill preserves the same Evermatter contract:

- Search before claiming prior context.
- Keep raw `Sources/` and `Evidence/` separate from compiled notes.
- Use compiled notes for durable conclusions only.
- Prefer `supersede_note` over silent rewrites when a conclusion changes.
- Treat review, stale, and contradiction signals as measurement, not judgment.
- Cite the pages, sources, or artifacts used.
