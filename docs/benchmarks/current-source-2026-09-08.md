<!-- authority:non-specification -->

# Current-source baseline — 2026-09-08

This baseline does **not** establish parity. The full-size Exomem idle workload
repeatedly refused edits while graph maintenance held the mutation boundary.
The media workload stopped at its first preservation acknowledgement in every
repetition. The common comparison also found availability failures, and one Basic
Memory run never established initial search-index membership within its bound.
The common collection is therefore **invalid as a complete three-pair comparison**;
its completed observations remain useful diagnostic evidence.

The [tracked observations](current-source-2026-09-08.json) retain every measured
call interval and outcome, per-cycle correctness, source/fixture/driver identities,
runtime packages, host load and graph proofs. Raw logs and generated evidence are
attached to [workflow run 34205614134](https://github.com/Artexis10/exomem/actions/runs/34205614134).
Both downloaded artifacts' SHA-256 hashes match their Actions digests recorded in
the JSON. Actions retention ends on 2026-10-08; the tracked observations remain
in Git.

## Exomem idle and media attempts

The measured source is `04dce82d64f9037b86604872b22045b2c34349c0`, with production
code from main `07ec3232450b5c93a6d0b161aacf1ef9180d4ed4` (version 0.75.0).
Each run starts with 3,800 fixture pages and executes 40 edit/read/search cycles.
One fresh four-CPU Linux runner runs the six cases serially in alternating pair
order. Python is 3.13.14; embeddings and CLIP are disabled. See the
[method guide](current-source-memory.md) for the configured workload and pins.

| Run | Edits acknowledged / attempted | Edit attempt median, ms | Successful edit median, ms | Read median, ms | Search median, ms | Foreground completion, s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 idle | 24 / 40 | 4973.84 | 439.42 | 12.65 | 134.44 | 128.84 |
| 1 media attempt | 23 / 40 | 5047.34 | 442.06 | 15.79 | 134.08 | 134.35 |
| 2 media attempt | 35 / 40 | 997.76 | 986.77 | 31.28 | 354.51 | 81.88 |
| 2 idle | 24 / 40 | 5049.13 | 435.15 | 12.48 | 135.41 | 130.15 |
| 3 idle | 24 / 40 | 4924.51 | 426.13 | 15.27 | 131.06 | 128.34 |
| 3 media attempt | 25 / 40 | 4994.79 | 438.80 | 13.74 | 136.47 | 129.45 |

All 240 direct reads and 240 keyword searches matched the last acknowledged
tracker edit. A refused edit leaves the previous accepted body as the expected
state; these checks do not count the refused change as committed. Every edit
refusal was `MUTATION_BUSY`, with `epistemic_graph_drain_paths` reported as the
holder of the mutation boundary. The three idle runs each refused 16 of 40
edits after approximately five seconds of waiting. No idle or media workload
passes the availability gate. Successful-call medians cannot be used to hide
those refusals. Per-run p95 is omitted because each operation has fewer than
100 samples.

All three idle runs eventually proved coherent full graph membership and source
hashes, a matching checkpoint/acknowledgement and the expected typed relation,
with the deferred graph and full-index queues empty. Observed catch-up after foreground completion was
12.62, 12.60 and 12.70 seconds. Those bounds include polling and the full-source
proof itself; they are not graph-engine execution times. One semantic queue item
remained in each final proof, so this does not claim every projection converged.

All three media attempts received
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` from their first
`preserve_artifacts` call. The response declared a committed mutation but did not
provide a usable exact terminal acknowledgement. The driver retained that failure
and did not resubmit under a different identity. None reached the declared four
groups of PDF/OCR work, a proven completed media window or a final graph proof.
No `process_media` call was reached in these attempts.
Their timings describe failed workload attempts and cannot measure the cost of
the intended complete ingestion workload.

## Common Markdown attempts

Both builds use the same 3,800 generated Markdown pages, 40 sequential cycles
and eight simultaneous appends. The Basic Memory revision is
`368e607622e9af3d982d0429cb48cfc6c83521f1`, reporting
`0.23.3.dev275+368e6076`; it is the fetched main source with FastMCP 4.0.3,
not the older 0.23.2 released wheel. Exomem uses the source above. These six runs
share a second fresh four-CPU runner, separate from the mixed-load host.

| Run | Accepted edits | Exact reads | Immediate search matches | Accepted appends | Edit attempt median, ms | Read median, ms | Search attempt median, ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 Exomem | 36 / 40 | 40 / 40 | 40 / 40 | 2 / 8 | 228.11 | 7.08 | 123.01 |
| 1 Basic Memory | Not reached | Not reached | Not reached | Not reached | — | — | — |
| 2 Basic Memory | 40 / 40 | 40 / 40 | 36 / 40 | 2 / 8 | 3973.03 | 2481.24 | 939.32 |
| 2 Exomem | 34 / 40 | 40 / 40 | 40 / 40 | 2 / 8 | 224.32 | 7.22 | 122.17 |
| 3 Exomem | 36 / 40 | 40 / 40 | 13 / 40 | 2 / 8 | 1144.15 | 39.51 | 569.52 |
| 3 Basic Memory | 40 / 40 | 40 / 40 | 38 / 40 | 2 / 8 | 4097.08 | 2388.98 | 970.03 |

Every completed run retained each acknowledged append exactly once. The other
six appends in each run were explicitly refused; none is silently counted as
accepted. All direct sequential reads matched the last acknowledged edit. Each
completed run eventually found its final marker and proved the typed relation
replacement, including absence of the previous target.

All three Exomem runs refused the unique crash-precondition write with
`MUTATION_BUSY` while the graph drain held the mutation boundary. The driver
therefore did not kill or restart those servers; this cohort provides **no
full-size Exomem crash-recovery result**. Basic Memory's two completed runs
acknowledged that write, survived the process-crash test and passed immediate
and eventual restart search and graph checks. This v1 driver proves public
accepted-content recovery, without a separate file-materialization probe.

Basic Memory run 1 had all 3,800 expected entity paths but did not prove the
required matching search-index identities by the 600-second startup bound.
It is retained as invalid setup evidence with zero measured foreground calls.
Runs 2 and 3 established that proof near the same deadline; startup including
initial graph proof was 619.75 and 618.92 seconds. Exomem startup was 78.66,
79.95 and 74.72 seconds. These numbers include initialization and proof work.
The logs and pinned source explain the long initial work: Basic Memory adds
permalink/frontmatter metadata to the plain Markdown fixture, while its watcher
indexes those file changes alongside the initial import. Exact search membership
became visible before its native completion stamp. The first measured edit in
each completed Basic Memory run overlaps the last two to three seconds of
startup; the other 39 edits and the later immediate search misses occur after
native completion. The medians describe this running, heavily linked workload,
including normal watcher work. Future runs require the native completion stamp
and use a 900-second startup bound for both products; the invalid attempt above
is not retroactively given a longer budget.

Observed relation replacement bounds were 44.16, 22.99 and 76.36 seconds for
Exomem and 2.15 and 2.39 seconds for Basic Memory's completed runs. Exomem's
proof hashes the full corpus; Basic Memory's proof checks the affected source
and relation generation. These are different proof costs, so the bounds cannot
be compared as raw graph execution times. The failed availability and incomplete
cohort also prevent a general cross-product speed claim from these medians.

Basic Memory's later search misses align with watcher refreshes of the tracker.
Its pinned search service commits removal of old search entries before inserting
replacements in another transaction. A visible gap is therefore a strong
explanation, rather than a proven transaction trace: the original observations
do not retain returned hit details or a final database. The two completed Basic
Memory runs and Exomem's third run record nonmatching successful search responses;
v1 cannot distinguish empty results, stale snippets and wrong hit identities
after the fact. The updated driver retains content-free hit counts and text
hashes on failed immediate searches. See the pinned Basic Memory
[index completion](https://github.com/basicmachines-co/basic-memory/blob/368e607622e9af3d982d0429cb48cfc6c83521f1/src/basic_memory/index/local_project.py),
[normalization](https://github.com/basicmachines-co/basic-memory/blob/368e607622e9af3d982d0429cb48cfc6c83521f1/src/basic_memory/indexing/batch_indexer.py)
and [search refresh](https://github.com/basicmachines-co/basic-memory/blob/368e607622e9af3d982d0429cb48cfc6c83521f1/src/basic_memory/services/search_service.py)
implementations.

## File materialization smoke evidence

The v2 driver separately checks the exact Markdown file after the owned server
has exited, without inserting a file check before SIGKILL. Three supplementary
local runs used 12 pages, two sequential cycles and three concurrent appends on
a busy host. They are correctness smokes, not a latency comparison.

Exomem's file contained its accepted crash write after exit and after restart.
Its run still failed on two refused concurrent appends and an immediate restart
search refusal; eventual search and graph checks passed. Basic Memory's file
was stale after exit, then matched the accepted body after restart. Its public
accepted-body read, search, graph and all three appends passed in this small run.
The tracked JSON keeps both file hashes, expected-body hashes, checks, calls and
actual driver/source identities. A second Basic Memory smoke then exercised the
corrected native-startup gate: it proved the completion stamp, all 12 entity and
search rows, and zero identity mismatches. That run accepted two of three appends;
all other checks passed, and the file was already current after exit. The two
Basic Memory observations show that the file can finish before or after the kill.
Earlier full-size v1 results retain their original proof coverage.

## Consistency boundaries

The common workload attempts public accepted-body and process-crash recovery
checks; the cases actually reached are listed above. It does not establish
power-loss durability or every concurrent mutation schedule.
The common workload covers edits, relation replacement and concurrent appends;
create, delete and move are not current-source comparison cases in this cohort.

The pinned Basic Memory source accepts note content in a database transaction,
then schedules guarded Markdown materialization through a bounded worker pool.
Its startup recovery re-drives pending, writing and failed materializations.
Public accepted-content availability and completed filesystem materialization
are therefore separate outcomes. See its pinned
[mutation service](https://github.com/basicmachines-co/basic-memory/blob/368e607622e9af3d982d0429cb48cfc6c83521f1/src/basic_memory/services/note_content_writes.py)
and [local materialization/recovery implementation](https://github.com/basicmachines-co/basic-memory/blob/368e607622e9af3d982d0429cb48cfc6c83521f1/src/basic_memory/index/note_content_materialization.py).
The original common driver does not separately prove final Markdown-file
materialization, so its public-read results must retain that limitation.
