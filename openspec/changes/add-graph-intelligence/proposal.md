## Why

The typed graph is only as useful as its edges, and today nothing measures them
cheaply or reproducibly. The one relation count that exists runs inside
`schema_memory(subject="relations", operation="infer")`: it parses every page's Markdown
to count authored relation rows in five buckets, and it cannot see frontmatter-typed or
wikilink edges, entity types, structural errors, or the caller's release filter. On a
personal vault the numbers that matter were gathered by hand: about half of authored
relations are the generic `relates_to`, most pages carry no typed edge, and several
hundred pages are disconnected.

Two quick defects sit beside the missing census. `infer(save=true)` builds its
observed-deletion guard from raw labels, so one capitalised legacy row such as
`Supports`, or any unregistered label, blocks every save even though neither is
vocabulary a registry save can delete. And the graph offers no request-time algorithm
beyond one-hop expansion, so it cannot answer "how is A connected to B" or "what does
this decision rest on" without leaking through withheld pages.

This change makes edge quality measurable first, then adds graph algorithms that run only
over the caller's visible subgraph, and proves with a benchmark that the graph improves
answers at realistic edge quality.

## What Changes

- **Relation-quality census (group 1).** A counts-only census over one published graph
  snapshot: authored edges by status, generic share, typed and specific coverage,
  predicate use, extension use, disconnected and isolated pages, entity coverage,
  unregistered pressure, inverse duplicates and seven structural false-precision checks,
  each with its own denominator. It reads no Markdown, runs no model and writes nothing.
  It runs under the caller's release filter inside the walk, reports `unavailable`
  rather than zero, and names no path, title or vault key in counts mode.
  - Surfaces: `schema_memory(subject="relations", operation="census", detail)`, the CLI
    `exomem relations census [--json] [--keys] [--sample N] [--judged FILE]`, which asks a
    running managed service over REST first and otherwise opens the sidecar read-only,
    and one doctor line.
  - An optional judged sample: a seeded, family-stratified sample of specific authored
    edges written locally as refs only, and a fold of an agent's verdicts into
    `false_precision_judged` with a 95% Wilson interval.
  - `infer`'s relation census is read from the snapshot when it is current, with its keys
    unchanged.
- **The `infer(save=true)` guard (group 1).** The observed-deletion guard protects only
  observed labels that resolve to a currently registered extension or alias; core and
  unregistered labels, in any case, no longer block a save.
- **Later groups.** One admission kernel and the request-time algorithms (connection
  paths, provenance trace and dependants, stale basis, per-hit support), the dreamer's
  graph families, and the `graph_reasoning` benchmark family with its acceptance run.
  Their tasks are listed here and specified when their lanes land.

## Capabilities

### New Capabilities

- `relation-quality-census`: the counts-only census, its surfaces, its egress rule, the
  judged sample, and `infer`'s delegation to it.
- `graph-intelligence` (later groups): the admission kernel, the egress rule for
  multi-hop results, and the algorithms and their surfaces.

### Modified Capabilities

- `epistemic-relation-registry`: the observed-deletion guard on `infer(save=true)`
  protects registered vocabulary in use, not raw labels.

## Impact

- Code: new `src/exomem/relation_census.py`; `schema_memory` gains the `census` operation
  and a `detail` parameter; a new `exomem relations` CLI command; a `relations.census`
  doctor check; `memory_schema.infer_relation_registry` delegates its census; the infer
  save path narrows its observed set.
- Contract: the `schema_memory` input schema gains `detail`, so the pinned tool-surface
  fingerprint advances and the connector contract records it as pending refresh.
- Egress: the census selector is a read-only `structure` surface; every count is taken
  inside the caller's release filter, and the graph generation is reported only to an
  unrestricted caller.
- Default-off and soft-fail: the census is operator-invoked and never runs on a request
  path; an unavailable graph yields a typed `unavailable` answer, and the CLI falls back
  from the service to a read-only local snapshot.
