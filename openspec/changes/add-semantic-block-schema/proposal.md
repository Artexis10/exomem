# add-semantic-block-schema

## Why

Exomem has claim extraction, context packs, evidence, audit, and supersession,
but it does not yet have a general Markdown-readable semantic block layer that
agents can parse and validate without inventing a separate storage model. This
change makes typed epistemic structure explicit while keeping ordinary Markdown
as the source of truth.

The goal is not to copy Basic Memory's generic note grammar. Exomem's moat is
governed cognition: claims, findings, evidence, decisions, assumptions, risks,
records, cases, requirements, actions, and their typed relations can be read by
humans, parsed deterministically, and reused by existing claim/context-pack
machinery.

## What Changes

- Add a semantic block parser/model/validator over normal ATX Markdown headings
  plus optional plain metadata bullets.
- Support the first block vocabulary: `claim`, `finding`, `evidence`,
  `decision`, `assumption`, `inference`, `constraint`, `risk`,
  `open_question`, `hypothesis`, `result`, `metric`, `failure`, `pattern`,
  `record`, `case`, `timeline_event`, `requirement`, `action`, `definition`,
  and `procedure`.
- Support typed relations: `supports`, `contradicts`, `refines`, `supersedes`,
  `derived_from`, `depends_on`, `evidenced_by`, `used_for`, `mitigates`,
  `causes`, `blocks`, `resolves`, `cites`, `implements`, `tests`, and `owns`.
- Add focused tests and docs for the Markdown shape, validation behavior, and
  compatibility guarantees.
- Integrate lightly with existing code: claim extraction may prefer semantic
  `claim` blocks, and context packs may expose parsed semantic blocks when
  present.

Pure-substrate note: this is deterministic parsing and validation only. It adds
no server-side reasoning model, no confidence scores, no new sidecar, and no
generated summaries.

## Capabilities

### New Capabilities

- `semantic-block-schema`: Markdown-readable semantic blocks and typed
  relations for Exomem notes.

### Modified Capabilities

- `context-packs`: packs may include parsed semantic blocks for packed pages
  while preserving existing fields and ordering.

## Impact

- New module: `src/exomem/semantic_blocks.py`.
- Tests: `tests/test_semantic_blocks.py`, plus targeted claim/context-pack
  coverage.
- Docs: `docs/semantic-blocks.md`.
- Limited integration points: `src/exomem/claims.py` and
  `src/exomem/context_pack.py`.
- No dependency, CLI, REST, MCP, storage, or migration changes.
