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
vocabulary a registry save can delete. The existing graph walker already supports
bounded neighbourhoods up to five hops; it is not an exact path/pattern query interface
with admitted evidence chains, reverse-impact witnesses and completeness guarantees.

This change makes edge quality measurable first. Request-time algorithms and their
admission, explanations and acceptance are owned by sibling
`add-graph-traversal-queries`, sharing `add-collection-query-engine`'s IR and executor.
Its evidence-chain and reverse-impact workflows test usefulness at realistic edge
quality; this change does not create another graph engine.

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
- **Later groups.** Request-time connection paths, provenance trace/dependants, stale
  basis and context explanations are tracked through sibling G1–G5. Existing dreamer
  families own graph upkeep; hubs/community analytics remain deferred. Graph-quality
  acceptance reuses sibling G6's benchmark effort and retains sparse/unavailable controls.

## Capabilities

### New Capabilities

- `relation-quality-census`: the counts-only census, its surfaces, its egress rule, the
  judged sample, and `infer`'s delegation to it.
The later graph workflows use the sibling `graph-traversal-queries` capability rather
than introducing a parallel admission/execution capability here.

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
