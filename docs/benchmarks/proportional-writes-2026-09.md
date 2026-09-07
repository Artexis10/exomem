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
index. The third Basic Memory startup was interrupted when this defect was
identified; no 8,000-page run began in this cohort.

[Incomplete-index diagnostics](proportional-writes-2026-09/incomplete-index-diagnostics.json)
retain every completed timed call, phase, correctness result, runtime and host
provenance, and the post-run index observations. They are **invalid for indexed
corpus-size comparison**; no comparative median or parity claim is accepted.
Exomem's three workflow times were 17.50, 16.75 and 17.17 seconds. Its first
creation previews were below one second, so commit work is the next diagnostic
focus. A separate instrumented run placed the remaining edit cost mainly in
the guarded canonical batch and index fan-out; semantic preflight and mutation
lock acquisition were small in that observation.

The corrected setup must prove the complete metadata and text-index fixture
membership for both products before timing. Valid after-change paired
observations and full convergence evidence are pending.
