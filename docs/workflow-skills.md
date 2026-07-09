# Workflow skills

Workflow skills are named agent workflows that sit above Exomem's typed tools.
They are small `SKILL.md` files that tell an agent when to activate, which
Exomem capabilities to use, what invariants to preserve, and what output shape
to return.

## How the layers differ

| Layer | What it is | Example |
| --- | --- | --- |
| Exomem tools | Typed MCP/REST/CLI operations that read and write the KB | `find`, `get`, `note`, `add`, `replace`, `attention` |
| Context packs | Retrieval-time evidence bundles returned by `find(pack=true)` | top hits, extracted claims, graph neighborhood, contradiction signals |
| Knowledge packs | Product/domain guidance selected during setup | technical, creative, legal/warranty, personal records |
| Workflow skills | Agent-visible workflows for common user intent | `exomem-continue`, `exomem-capture`, `exomem-review` |

Packs guide what domain a user is working in. Workflow skills guide what job the
agent is doing right now.

## Built-in workflow skills

- `exomem-continue` - resume prior project or session context.
- `exomem-capture` - save a durable conclusion without dumping transcripts.
- `exomem-ingest` - preserve an external artifact, then compile what matters.
- `exomem-research` - gather sources and save attributed findings.
- `exomem-reflect` - extract decisions, failures, patterns, open questions, and next actions.
- `exomem-curate` - improve links and compiled-note quality safely.
- `exomem-defrag` - reconcile duplicate, stale, or conflicting memory.
- `exomem-review` - drain attention and audit queues.
- `exomem-media` - search and inspect PDFs, images, audio, video, and other artifacts.

## Installation and discovery

The canonical definitions ship in:

```text
src/exomem/_scaffold/_Schema/workflow-skills/
```

`exomem init` copies them into new vaults under
`Knowledge Base/_Schema/workflow-skills/`.

`exomem install-skill` installs the core `exomem` skill and also installs each
workflow skill as a sibling Claude Code skill folder, so clients that support
skills can discover them directly.

Generic MCP clients that do not support skills should call `bootstrap()` once.
The bootstrap payload includes a compact `workflow_skills` index with names,
purposes, trigger examples, and the canonical vault-relative path.

## Invariants

Every workflow skill preserves the same Exomem contract:

- Search before claiming prior context.
- Keep raw `Sources/` and `Evidence/` separate from compiled notes.
- Use compiled notes for durable conclusions only.
- Prefer `replace` over silent rewrites when a conclusion changes.
- Treat review, stale, and contradiction signals as measurement, not judgment.
- Cite the pages, sources, or artifacts used.
