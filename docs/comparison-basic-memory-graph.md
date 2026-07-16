# Exomem vs Basic Memory: graph-value comparison

Measured 2026-07-16 against Exomem source package version `0.23.0` at the
post-tag revision `7e89a3f7bff7` and Basic Memory `0.22.1` at pinned revision
`0e59bbffaf7d`. The Exomem revision is current `main`, not the already-published
`v0.23.0` artifact.

## Verdict

Exomem is demonstrably superior on the graph-dependent tasks in this benchmark.
It matches Basic Memory's native graph on ordinary one-hop reachability,
multi-hop reachability, and relation-type fidelity, then wins on direction,
distractor exclusion, traversal lenses, provenance traceability, active versus
superseded state, and relation-bearing semantic blocks.

That is a graph claim, not a claim that Exomem is globally the better product.
This benchmark says nothing about cloud onboarding, generic search UX, canvas
workflows, imports, or ecosystem breadth.

The broader lean local-core run is also green: Exomem passed all 22 required
public-path probes across shared core, lifecycle integrity, explanation truth,
and Exomem extensions. Basic Memory passed 11 probes, failed the expected graph
precision probe, and returned 10 explicit unsupported results for Exomem-only
contracts. The harness deliberately emitted no full local-core-advantage claim:
lean mode disables learned models, performance sampling, and media.

| Lean gate | Result |
|---|---:|
| Required probe-ID coverage | 22/22 for each contender |
| Exomem operation execution | 21/21 active MCP operations; CLI surface explicitly mirrored |
| Basic Memory operation execution | 16/16 active MCP and 5/5 active CLI operations |
| Shared core | Pass |
| Lifecycle integrity | Pass |
| Explanation truth | Pass |
| Exomem extensions | Pass |
| Performance envelope | Not run in lean profile |
| Full local-core advantage | Not emitted |

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

The authored Markdown trees were unchanged by both contenders. Exomem startup
created four recorded media-extraction sidecars, all classified as derived
artifacts rather than authored content. Recorded initial corpus hashes were
`57ee993e0d25f6a384d1f227fee91b7e8cf8b69ecf1cda7909cc8725fcdea298`
for Exomem and `d4141c8adbbb423c77cfefd417daf0400a527c13744be682cabd1ee0ca7ad752`
for Basic Memory. Lean index duration was 0.947 seconds for Exomem and 6.873
seconds for Basic Memory. Graph-case response totals were 43,656 versus 28,486
bytes; aggregate graph-case latency was 143 versus 2,461 milliseconds. These
efficiency numbers are informational and do not affect any correctness gate.

## Fairness contract

The benchmark starts from one versioned, product-neutral manifest: 34 notes, 10
relations, one lifecycle replacement pair, semantic units, nested metadata,
schemas, deterministic media artifacts, deliberate distractors, 11 graph tasks,
and 27 layered probes. It then renders each product's documented native
Markdown:

- Exomem receives its generic schema scaffold, governed frontmatter, semantic
  blocks, and canonical `## Relations` bullets.
- Basic Memory receives native frontmatter, atomic observations, and open
  relation bullets. Lifecycle and block facts are represented as far as its
  format allows; missing return contracts are reported as unsupported rather
  than silently removed.

The direct comparison performs a profile-appropriate full filesystem index for
both contenders, then serves agent-facing cases through one persistent stdio MCP
session per product. Genuine maintenance-only operations remain labelled CLI.
Basic Memory uses a benchmark-managed environment and isolated
home/config/SQLite/cache/project state with file mutation disabled. The report
records revisions, lock/config hashes, runtime inventory, corpus hashes, raw
request/response envelopes, response bytes, latency, index duration, verified
renderer fact parity, operation-to-probe execution, per-probe state hashes, and
derived-sidecar changes. The recorded lean run preserved 242 scrubbed raw
artifacts for 121 public calls. Its lifecycle probe rejects a schema-invalid
public write, follows one stable unit reference through add/update/remove while
checking old text and categories disappear, and compares incremental reconcile
with a full public maintenance sweep. The direct-edit probe verifies the exact
contract finding identity before repair.

## Reproduce

The fast Exomem-only fixture gate requires no Basic Memory checkout or model:

```bash
uv run python scripts/graph_value_benchmark.py
```

For the direct comparison, place a current Basic Memory checkout beside Exomem:

```bash
uv run python scripts/graph_value_benchmark.py \
  --direct \
  --profile lean \
  --basic-memory-root ../basic-memory \
  --request-timeout 180 \
  --output-json /tmp/graph-value.json \
  --output-markdown /tmp/graph-value.md
```

Raw envelopes are written under `<work-dir>/raw-artifacts/`; contender databases,
configs, caches, and logs remain under the same disposable work directory. Direct
mode is intentionally desk-side. Quiesce other model/index jobs first, use the
pinned sibling revision from the manifest, and never point either contender at a
live vault. A highly restricted process sandbox may block asynchronous stdio even
when both servers are healthy.

The full profile is explicit:

```bash
uv run python scripts/graph_value_benchmark.py \
  --direct \
  --profile full \
  --basic-memory-root ../basic-memory \
  --request-timeout 600 \
  --work-dir /tmp/exomem-full-local-core \
  --output-json /tmp/exomem-full-local-core/report.json \
  --output-markdown /tmp/exomem-full-local-core/report.md
```

### Full-profile status

No corrected full-profile claim is recorded from this host. The isolated WSL2
runtime aborts with native `SIGABRT` (`munmap_chunk(): invalid pointer`) while
loading `BAAI/bge-base-en-v1.5` through SentenceTransformers, on both CPU and
CUDA and in two separate virtual environments. The cached weight's SHA-256
matches its content-addressed model blob, and direct safetensors inspection can
read its tensors. This is therefore recorded as an environment/setup failure,
not an Exomem loss, and the harness emits no report or claim from that attempt.

A prior pre-correction full diagnostic remains useful only for backlog evidence:
its performance gate failed (Exomem/Basic Memory query median ratio 5.527 against
the 2.0 ceiling; p95 ratio 3.320 against 2.5), and PDF/image/audio/video probes
were unavailable because the required local extras were absent. Its retrieval
failures exposed harness setup mistakes that are fixed here: both products now
build full embedding indexes, Exomem uses the public maintenance rebuild, and
the lexical explanation probe requests a mode that actually exposes BM25 raw
scores. Because the corrected full run could not pass native model setup, those
fixes are covered by focused tests but do not upgrade the historical diagnostic
into a current full-profile result. The corrected harness now uses
content-bearing OCR, speech, PDF, and video fixtures; searches the extracted
semantic text; fingerprints CLIP and ASR identities/runtime policy; compares
CLIP vector and hybrid evidence; and takes its cold sample as the first public
query after indexing. BM25 and temporal isolation are explicitly marked
unsupported because the public API exposes them only inside hybrid/query-intent
retrieval; keyword and text-vector lanes are isolated, and graph is proved by a
same-request controlled difference. HTTP upload transport is explicitly outside
this stdio knowledge-behavior benchmark and is not claimed.

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
comparison optional. Learned retrieval, paired performance, and media remain
unproved by the corrected run until a valid full-profile environment completes;
the lean result must not be generalized to those capabilities, hosting, or
overall product superiority.
