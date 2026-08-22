# Proposal: add-referent-resolution

## Why

Ordinary recall returns a flat page list for vague relational phrases such as
"my two coastal friends". Authored person entities and typed edges may already
identify one person, but graph corroboration of a lexical candidate is not
expressed to the agent and recall has no expected-count or abstention semantics.
That makes an unrelated person hit look as plausible as the represented person.

## What Changes

- Add a deterministic, model-free cue detector and a read-only referent stage
  after release annotation in `op_find`.
- Enumerate authored entities through a checkpoint-keyed per-process registry,
  compose categorical exact/fuzzy/retrieval/graph/attribute evidence, and emit
  explicit resolved, partial, ambiguous, or unresolved state.
- Keep the stage default-on only for detected cues, soft-fail by omission, and
  provide `EXOMEM_DISABLE_REFERENTS` as a kill switch.
- Add governance filtering for entity and evidence paths before emission.
- Add a seeded graph-on/graph-off benchmark and bounded 2k/8k latency gates.
- Document aliases, canonical `about_entity` topic edges, and agent handling of
  partial/ambiguous/unresolved results.

This remains a pure substrate measurement over authored pages, attributes, and
edges: it runs no reasoning model and writes no knowledge.

## Capabilities

### Added Capabilities

- `referent-resolution`: deterministic, abstention-aware entity resolution over
  released recall hits and authored typed graph evidence.

### Modified Capabilities

- `find-recall-efficiency`: the optional stage is post-cache, timed, and bounded.
- `graph-find-ranking`: graph corroboration respects release decisions and is
  independently ablatable without changing ranking.
- `agent-bootstrap-contract`: agents learn how to handle referent outcomes.
- `command-surface`: `referents` is an additive envelope key on existing find,
  ask-memory, CLI, REST, and MCP leaves; no parameter is added.

## Impact

- New pure/runtime modules, a checkpointed entity registry, one governance
  guard, and additive `op_find` envelope wiring.
- Existing hit ordering, cache keys, tool schemas, write paths, and ranking
  intent/scoring remain unchanged.
- New synthetic fixtures, benchmark script, latency coverage, scaffold guidance,
  and focused lint/type-check coverage.

Deliberately out of scope: entity-emergence sensor (f21/S5), graph-lane ranking
changes, referent intent, `aliases` on create-entity, model-backed matching.
