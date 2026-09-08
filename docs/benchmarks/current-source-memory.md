<!-- authority:non-specification -->

# Current-source memory observations

The **Mixed-load observations** workflow collects two independent cohorts on
fresh Linux runners. Both are diagnostic jobs: an observed product failure is
retained evidence; missing or invalid observations fail the collection job.

| Cohort | Workload | Default repetitions |
| --- | --- | --- |
| Common Markdown | Marker edits, immediate reads/searches, typed relation replacement, concurrent appends, acknowledged write followed by process crash and restart | Three alternating Exomem/Basic Memory pairs |
| Exomem media contention | Identical foreground edit/read/search loops with and without pinned PDF/OCR ingestion | Three alternating idle/media pairs |

Each run starts with 3,800 generated pages in new directories outside both
source checkouts. The common loop has 40 cycles and an eight-request concurrent
append burst. The media loop has 40 cycles and four groups of one PDF and two
OCR images. Embeddings are disabled. Each cohort has its own runner; do not pool
samples across the two jobs as though they shared a host.

The comparison pins Basic Memory's source revision in
`scripts/parity_basic_memory.py`, validates its current SQLite schema, and uses
that revision's frozen lockfile. Exomem uses the tested PR head and its lockfile.
Reports retain actual imported source identity, dependency inventories, fixture
and driver hashes, startup attempts, every measured call, failed checks and
server logs. Historical released-wheel measurements remain separate.

An explicit conflict refusal can fail the all-requests-accepted check while
passing the acknowledged-content check. This distinguishes availability from
silent data loss. Immediate search and eventual visibility are also separate
checks. Graph results combine bounded public edge visibility with read-only
current-generation and old-edge-absence proofs; convergence times include
polling and proof cost.

Body equality ignores opening metadata YAML and one terminal linefeed, because
the two writers differ on that convention. All other body bytes remain
significant. SIGKILL checks process-crash recovery, not power-loss durability.
Per-operation p95 is omitted below 100 observations; small samples retain their
median and maximum.

The workflow runs when an in-repository `perf/mixed-load-*` PR opens or reopens,
or through manual dispatch. It uploads `mixed-load-observations` and
`current-source-parity-observations`. Each run has `result.json`, `host.json`,
stdout/stderr and server logs; the cohort manifest retains the declared order
and invalid results. The collection never deploys or restarts a live service.

For local reproduction on an otherwise idle machine, use the two collectors'
`--help` output. Commit the measured driver first and select a new output path
outside both repositories. The current-source collector requires the pinned
Basic Memory checkout and that checkout's Python executable.
