# Exomem vs Basic Memory: graph-value comparison

Measured 2026-07-11 against Exomem revision `6ce9499bfe6b` and Basic Memory
`v0.22.1-17-g0e59bbff` at revision `0e59bbffaf7d`.

## Verdict

Exomem is demonstrably superior on the graph-dependent tasks in this benchmark.
It matches Basic Memory's native graph on ordinary one-hop reachability,
multi-hop reachability, and relation-type fidelity, then wins on direction,
distractor exclusion, traversal lenses, provenance traceability, active versus
superseded state, and relation-bearing semantic blocks.

That is a graph claim, not a claim that Exomem is globally the better product.
This benchmark says nothing about cloud onboarding, generic search UX, canvas
workflows, imports, or ecosystem breadth.

## Recorded result

| Independent dimension | Exomem | Basic Memory | Reading |
|---|---:|---:|---|
| One-hop reachability | 1/1 | 1/1 | Parity |
| Multi-hop reachability | 2/2 | 2/2 | Parity |
| Relation-type fidelity | 1/1 | 1/1 | Parity |
| Direction fidelity | 2/2 | 1/2 | Exomem excludes the incoming neighbor under an outgoing traversal |
| Distractor precision | 1/1 | 1/2 | Exomem's typed filter excludes the unrelated edge |
| Traversal-lens filtering | 2/2 | 0/1 unsupported | Exomem exposes relation-family traversal profiles |
| Provenance traceability | 2/2 | 0/1 unsupported | Exomem returns authored origin and source anchor |
| Supersession handling | 3/3 | 0/1 unsupported | Exomem returns the edge plus active/superseded state |
| Semantic-block precision | 2/2 | 0/1 unsupported | Exomem returns the claim block anchor and its edge |

The dominance gate passed. It has no weighted aggregate: Exomem must be no worse
on every common graph dimension, pass every fixture invariant, and strictly win
provenance, supersession, and semantic-block precision. A future Exomem
reachability regression makes the result false even if all governance dimensions
still pass.

The generated Markdown trees were unchanged by both contenders. Recorded corpus
hashes were `b993b570bfaa96cb8fa18bc532390a4a17091263f9aac7aa7268bb8a5f7ab106`
for Exomem and `5c528f82eb7e1385c4865cdc9920a2a30e802a6303735912ee02a508685fdd10`
for Basic Memory.

## Fairness contract

The benchmark starts from one versioned, product-neutral manifest: 17 notes,
seven ordinary directed relations, two provenance relations, one lifecycle
replacement pair, one relation-bearing claim block, deliberate distractors, and
nine graph tasks. It then renders each product's documented native Markdown:

- Exomem receives its generic schema scaffold, governed frontmatter, semantic
  blocks, and canonical `## Relations` bullets.
- Basic Memory receives native frontmatter, atomic observations, and open
  relation bullets. Lifecycle and block facts are represented as far as its
  format allows; missing return contracts are reported as unsupported rather
  than silently removed.

The direct comparison explicitly performs a full Basic Memory filesystem index
before measurement. Both products then serve every task through one persistent
stdio MCP session. Semantic/model features are off, Basic Memory uses an isolated
home/config/SQLite state with file mutation disabled, and versions plus revisions
are recorded. Response bytes and latency are emitted separately but do not affect
correctness or dominance.

## Reproduce

The fast Exomem-only fixture gate requires no Basic Memory checkout or model:

```bash
uv run python scripts/graph_value_benchmark.py
```

For the direct comparison, place a current Basic Memory checkout beside Exomem:

```bash
uv run python scripts/graph_value_benchmark.py \
  --direct \
  --basic-memory-root ../basic-memory \
  --output-json /tmp/graph-value.json \
  --output-markdown /tmp/graph-value.md
```

Direct mode is intentionally desk-side. A highly restricted process sandbox may
block asynchronous stdio even when both servers are healthy.

## What to improve next

The next target is graph activation and product use, not a larger schema.

1. Activate existing corpora through a governed propose/review/apply loop. Graph
   quality is currently constrained more by missing authored relationships than
   by missing relation vocabulary.
2. Make the graph advantage visible in normal recall and review: show the path,
   relation family, provenance anchor, and active/superseded state when they
   materially change the answer.
   *(Delivered for recall by `add-typed-graph-find-lane`: the `find` graph lane
   now expands through the typed sidecar by default — typed/provenance families
   ranked ahead of plain wikilinks — and graph-surfaced hits carry a `graph`
   annotation with relation type, direction, and seed in both compact and full
   envelopes. Review-side visibility shipped earlier via `review_item_context`.)*
3. Add a medium public corpus and user-task tier after observing real activation
   output. Keep the same independent dimensions and dominance rule.
4. Improve relation authoring and maintenance ergonomics: acceptance queues,
   unresolved-target repair, stale-edge review, and useful coverage telemetry.
5. Expand schema only when evidence shows a repeated semantic collision that
   changes traversal or review outcomes. A new relation should have a clear
   parent, a demonstrated task distinction, and enough observed examples to
   justify authoring and migration cost.

Schema remains strategically important as the graph's governance layer. It is
not the current bottleneck, and growing it speculatively would optimize the
easiest surface to expand rather than the hardest value to deliver.

## Limits

This is a small adversarial fixture, not an independent third-party benchmark or
a statistical sample of real vaults. Unsupported means the tested public
`build_context` response cannot return the required fact; it does not mean Basic
Memory has no graph or cannot store a similarly named relation. Basic Memory may
change, which is why the runner records its exact revision and keeps the direct
comparison optional.
