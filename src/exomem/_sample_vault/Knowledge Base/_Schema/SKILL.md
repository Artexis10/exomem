---
name: exomem
description: Minimal public sample schema for exomem smoke tests.
version: 0.1.1
---

# Knowledge Base Sample Schema

This is a deliberately tiny schema stub for the public sample vault. The full
starter schema ships in `src/exomem/_scaffold/_Schema/` and is installed by
`exomem init`.

The sample vault demonstrates the product layers in miniature:

- `Sources/` keeps raw captured input.
- `Notes/` keeps compiled knowledge.
- `Entities/` keeps reusable typed nodes.
- `Evidence/` keeps proof artifacts for a case or claim.

Typed relations follow the public governance loop: resolve with
`connect_memory(operation="resolve-relation")`, reuse a specific truthful match,
or choose an honest `relates_to`/no edge fallback. A durable recurring distinction
is proposal-first through `propose-relation` and hash-guarded `save-relations`;
meaning corrections create a new canonical key and deprecate the old one.
