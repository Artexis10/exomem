<!-- authority:non-specification -->

# Proportional-write measurements

The current-main reproduction takes **53.60 seconds** to complete and verify the
shared Markdown workflow at 8,000 pages. This is a baseline observation, not an
after-change result or a general product ranking. The implementation contract is
in the OpenSpec change `make-durable-writes-proportional`.

## Baseline reproduction

The source is immutable commit `9e2e6487`, running Python 3.13.14 with the frozen
lean dependency lock. `scripts/durable_closure_common.py --product exomem
--pages 8000 --timeout 300` generated fresh disposable vault and state roots.
The harness retains normal public creation previews, canonical commits, exact
reads and text-search convergence. Task test/benchmark workers were quiescent;
external host load was not sampled for this intake observation.

| Measurement | Time |
|---|---:|
| Startup and mutation readiness, outside the workflow clock | 138.33 s |
| First creation preview | 21.96 s |
| First creation commit | 9.27 s |
| First edit | 16.89 s |
| Second edit | 1.68 s |
| Second creation preview | 0.17 s |
| Second creation commit | 2.02 s |
| Whole workflow, including final public verification | 53.60 s |

[Baseline JSON](proportional-writes-2026-09/baseline-common-8000.json) retains the
corpus digest, runtime, call observations and verification. The deterministic
per-page inventory is omitted to keep the evidence reviewable.

A separate coarse timing run identifies the repeated work. Its first timed
preview spent 5.83 of 6.15 seconds rebuilding the writer resolver; the first edit
spent 14.54 of 16.21 seconds there, while semantic preflight took 46 ms. The
whole instrumented run took 33.93 seconds. The difference from the baseline is
why acceptance uses repeated paired observations rather than a single ratio.
These timings wrap functions in the disposable service process; they are
diagnostic and do not replace uninstrumented comparison runs.

The graph is still cold when this shared workflow begins: setup proves lexical
recall and mutation admission, and does not wait for a live graph sidecar.
Generations 1–4 register `graph_sync_predecessor_unreadable` because the first
private graph rebuild has not published. Those registrations coalesce; they are
not evidence of four simultaneous rebuilds. The after-change comparison retains
the same setup and reports optional graph state separately.

The topology discovery reproduction creates 200 source notes, only one linking
to a newly appearing target. Calling the existing `_sources_linking_to` method
with the current resolver returns the correct source but performs **200 body
reads, 199 unrelated**. The probe counts `read_bytes_without_pinning` only during
dependency discovery; it makes no claim about total graph publication work.

## Comparative reference

The preceding investigation's single paired observations are retained in the
[shared-workflow report](durable-closure-investigation-2026-09.md), but its
sentinel-only startup gate did not prove the indexed corpus size. Those pairs
must not establish large-corpus comparative latency. Basic Memory
is pinned to wheel version 0.23.2, SHA-256
`a1679a16319d8a7fb9c0486033551a47dedc0fbae7f5da81444eb3c4bf0ccecb`.
Its accepted-content storage and deferred file materialization differ from
Exomem's file-durable commit boundary; the comparison therefore verifies final
exact reads and public search convergence for both products.

## Incomplete-index setup diagnosis

The first integrated cohort used source `f7a20cef` and the unchanged common
driver. All five completed workflows passed exact body/read/search checks.
However, inspecting the run-owned stores exposed a setup defect:

| Product | Completed 3,800-file runs | Indexed metadata/search rows after closure |
|---|---:|---:|
| Exomem | 3 | 3,802 in each run |
| Basic Memory | 2 | 103 in each run |

The two timed creations account for Exomem's additional rows. Basic Memory's
sentinel could become searchable during its first indexing batch, allowing the
workflow to start before most fixture notes entered its resolver and search
index. The third Basic Memory run was interrupted when this defect was
identified. Its logs show public writes and reads had occurred, but no final
closure result was captured; no 8,000-page run began in this cohort.

[Incomplete-index diagnostics](proportional-writes-2026-09/incomplete-index-diagnostics.json)
retain every completed timed call, phase, correctness result, runtime and host
provenance, and the post-run index observations. They are **invalid for indexed
corpus-size comparison**; no comparative median or parity claim is accepted.
Exomem's three workflow times were 17.50, 16.75 and 17.17 seconds. Its first
creation previews were below one second, so commit work is the next diagnostic
focus. A separate instrumented run placed the remaining edit cost mainly in
the guarded canonical batch and index fan-out; semantic preflight and mutation
lock acquisition were small in that observation.

The corrected setup now proves complete metadata and text-index fixture
membership for both products before timing, including the actual vault/project
binding and supported FTS schema. An independent review reran all 45 focused
tests, wrong-vault and ordinary-table rejection cases, and real four-page
public workflows for both products.

## First complete-index pair

Source `c563ec57` includes the reviewed proportional changes and current-main
vocabulary work. Both products proved exactly 3,800 indexed fixture identities
before the unchanged public workflow. All body/read/search checks passed.

| Product | Complete startup, outside workflow clock | Verified public workflow |
|---|---:|---:|
| Exomem | 62.11 s | 17.41 s |
| Basic Memory 0.23.2 | 651.05 s | 11.53 s |

[Preliminary pair JSON](proportional-writes-2026-09/preliminary-complete-index-3800.json)
retains both exact membership proofs, timed calls, correctness checks, runtime
and host evidence. This is one valid pair, not an accepted comparative median.
The next Basic Memory setup was interrupted before its full-index gate could
pass, to investigate the remaining foreground cost before repeating acceptance
runs. No 8,000-page observation began in this cohort.

## Background contention diagnosis

Stack sampling during Exomem commits found private graph admission scans and
the initial due-state audit reading the corpus concurrently. Foreground CPU
time was substantially below elapsed time. A disposable prototype pausing
those scans for at most 50 ms per checkpoint during canonical commands took
11.65 s. Covering the complete foreground dispatcher invocation instead took
8.35 s; both diagnostic workflows passed exact body/read/search checks.

These are instrumented diagnostic observations, not shipping code or accepted
parity results. The OpenSpec amendment requires explicit scopes, exact-vault
isolation, bounded background progress, and bypasses for synchronous work and
explicit graph waiters. Source admission, durable custody and independent
publication proofs remain unchanged. Repeated uninstrumented comparisons,
real-media regression, warm-graph characterization and full-suite evidence are
still required for delivery.
