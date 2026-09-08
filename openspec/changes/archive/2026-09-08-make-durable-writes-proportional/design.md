## Context

See `proposal.md` for the motivation. The baseline is commit `9e2e6487`, measured
with `scripts/durable_closure_common.py` in an immutable disposable copy and
fresh state: 8,000 pages, 53,600.53 ms to verified closure. The first timed
creation preview took 21,956.95 ms; creation 9,271.70 ms; first edit 16,886.03 ms.
The later preview took 169.80 ms. The corpus cache reports event hits throughout.
A separate timing probe measured a writer resolver full build at 21,059.81 ms
while the resident corpus's map assembly took 50.97 ms. Profiling is diagnostic,
not a replacement for uninstrumented paired measurements.

`find.writer_resolver_snapshot` forks a current broad resolver, but on a cold or
stale broad cache it constructs `WikilinkResolver(root)`, rereading all Markdown.
`note.note` and `edit.edit` invoke it before semantic preflight. The already-warm
`SemanticCorpusContext` contains the same broad path/title inputs. Creation does
not prime the broad resolver; an existing-page commit eventually does, explaining
the later fast preview. Existing tests explicitly warm the broad resolver and
therefore miss the corpus-warm/broad-cold case.

The graph currently drops unresolved body links. `_topology_affected_sources`
scans all bodies for appeared targets; `_resolver_affected_sources` scans indexed
bodies comparing old/new resolution. Independent topology publication proofs
also reread source versions. Those proofs must not be silently replaced by an
assumption that a stored hash proves current disk bytes.

The Basic Memory 0.23.2 artifact already persists accepted note content and raw
relation targets. Its deferred materialization has different acknowledgement
semantics: a successful API write can precede Markdown materialization. Its
resolver scans current unresolved relation rows rather than performing an exact
indexed lookup for each target. This change borrows retained parsed state and
indexed dependencies, without adopting its different canonical-write boundary.

## Goals / Non-Goals

The deliverable is shared-workflow parity, with deterministic protection against
the two measured kinds of repeated work. The parity target is an Exomem median
verified-closure time no greater than the Basic Memory median at both 3,800 and
8,000 pages, using at least three paired fresh runs per size and alternating
product order. Failure to meet it requires further diagnosis within this change;
it is not permission to omit validation or report a single favorable run.

Canonical Markdown, governance, source closure, generation fencing, media custody,
and the existing queues remain authoritative. No new task scheduler, DB-first
canonical write protocol, model invocation, database tuning, or replacement
semantic corpus store is introduced. This is not a claim that every cold-start
or graph publication operation becomes O(changed); required independent source
proofs remain visible costs.

## Decisions

### Reuse the current semantic corpus without hydrating it during a preview

Add a narrow read-only accessor in `semantic_contract.py` that returns resident
resolver entries only when the requested freshness, live event checkpoint,
stored checkpoint, configuration census, and loaded registry/language identities
match. Recheck the relevant identity around snapshot acquisition so a concurrent
event cannot give stale entries a new caption. A supplied direct-disk freshness
key must match as well. Require exact equality between resident resolver-entry
paths and the live broad-vault Markdown membership. Semantic construction can
skip an unreadable non-governed file while the broad resolver retains its stem
with no title; decline reuse in that case. The accessor neither builds a cold corpus nor publishes
a resolver or pending destination. It returns `None` when proof is unavailable.

`find.writer_resolver_snapshot` first retains its existing current broad-cache
path, then tries these proven resident entries, constructing a detached resolver
with `from_entries`, then keeps the existing fresh fallback. All callers gain
the same behavior through this shared seam. Do not use the recall-only SQLite
catalog for broad writer metadata: it intentionally omits namespaces that can
participate in writer resolution. Do not prime a shared resolver during preview.

This reuses an existing complete parsed projection rather than adding another
durable corpus and its invalidation protocol. Copying path/title maps is cheap
relative to rereading and parsing every source, and keeps pending drafts isolated.

### Persist internal dependencies in the graph's existing SQLite transaction

Add an internal source-coverage table (source path, source hash, dependency
format/version and expected dependency count) and a raw-link table (source path,
normalized lookup key, original raw target). Deduplicate repeated identical raw
targets per source. Index lookup key plus source path. A page with no links still
gets an explicit coverage row. Bump the graph schema version and require complete
coverage before using these tables for bounded discovery.

Derive dependencies from the same admitted raw bytes and the existing body-link
recognizer used by topology discovery. Keep normalization compatible with
`normalize_wikilink`: brackets, whitespace, alias, anchor, optional `.md`, full
path, KB-stripped path, stem, and case-insensitive title. Lookup may conservatively
include extra candidates; it must never omit a potentially changed resolution.
For each changed path, query old/new full-path, KB-relative, stem, and title keys,
then run the established resolver on stored raw targets. Include existing inbound
edge sources to cover graph relations already represented as edges. Retitles and
ambiguity changes are topology changes even when the path still exists.

Create/replace the source's dependency records in `_index_path` inside the same
transaction as graph rows, after the existing source and admission recheck.
Remove them in deletion, policy removal and exact persisted-row purge paths.
Every full rebuild clears both dependency tables before repopulating through the
same producer, so orphan rows cannot survive an in-place rebuild. Private full
rebuilds populate them through the same producer. Do not add public
unresolved nodes or alter edge rendering. Raw Records never enter these tables.

Include dependency source identities in the semantic-isolation census and exact
purge routing (`audit.py`, `reconcile.py`), and include the new source rows in the
governance lifecycle's residual-row check. Audit raw targets and lookup keys as
authored dependency data with structural/normalization validation, not as file
identities: missing and ambiguous targets are intentionally retained. A corrupt
target/key row is removed together with its source's coverage claim in one
transaction, requiring repair; a quarantined source loses all its dependency and
coverage rows. Reopening and full rebuild must not leave ghosts or repeatedly
recreate invalid structural data. Preserve existing audit continuation and
incomplete-result behavior for older schemas.

Dependency audit pages advance by a unique persisted row identity, so a page
boundary cannot skip remaining targets from the same source. Bind continuation
to the database and nonempty WAL revision before and after reading the page;
normalize an absent and empty WAL equally and exclude derived SHM churn from
this revision identity. Retain the existing no-follow sidecar binding. A real
revision change invalidates continuation rather than captioning old rows with
a new revision; same-count updates must be detected.

### Replace dependency discovery without weakening publication proofs

Use the indexed dependency query in both `_topology_affected_sources` and
`_resolver_affected_sources`. Before accepting ANY result, positive or empty,
prove a bijection between all indexed file sources and coverage rows, current
dependency format, matching source hashes, and matching per-source row counts in
one graph snapshot. A positive hit cannot hide a second dependent source with
missing coverage. An old or incomplete sidecar returns
an explicit rebuild requirement or takes the existing safe recovery path; merely
creating empty tables cannot make old rows complete.

For incremental refresh, preserve `_resolver_source_versions`, membership checks,
source-version rechecks, retained event lineage, policy identity, and transactional
acknowledgement. These independent proofs still may read the corpus; the removed
work is the additional dependency-discovery scan. For deferred drains, retain
source checks for the selected batch and the current projection/generation proof.
Bounded discovery requires provable prior topology. In particular, preserve the
existing conservative fallback for deletion and outside-KB target changes when
the old resolver cannot be reconstructed. The KB-indexed graph has no authoritative
old title for an outside-KB target; its retitle/deletion can remove an ambiguity
without leaving an inbound edge. Return a rebuild requirement rather than trying
only the new title. No broad resolver-history store is introduced in this change.
Retained raw targets permit discovery after cache eviction or process restart.
The existing coalesced queue and compare-and-swap receipt retirement remain in use.

The observed `graph_sync_predecessor_unreadable` rebuild registrations require
separate diagnosis. Cold startup may legitimately have no usable predecessor;
this design does not authorize weakening that gate or treating every registration
as a bug. Any additional correction requires an evidence-backed design amendment.

### Two implementation lanes, then integrated evidence

The writer metadata lane owns `find.py`, `semantic_contract.py`, and focused
resolver/cache tests. The dependency lane owns `epistemic_graph.py`, an optional
private dependency helper module, dependency integration in `audit.py`,
`reconcile.py`, `governance/lifecycle.py`, and focused graph/isolation tests. They start only after
the design critique is resolved. Their production seams are independent; the
root owns all OpenSpec and benchmark changes and integrates reviewed patches.

Each lane works red-first in its own linked worktree and disposable state. An
independent reviewer runs its important reproductions in a separate clean copy.
The combined patch receives one fresh integration review. Scoped tests run during
iterations; one successful full lean corpus plus existing latency/privacy/spec
gates runs at the delivery boundary. Real media regression checks use the existing
public benchmark and isolated state, with no live-cell mutation.

### Prove the whole fixture is indexed before comparing corpus-scale writes

The first integrated comparison exposed a setup defect in the existing shared
driver: its sentinel search can succeed after Basic Memory indexes its first
100-file batch. Two completed 3,800-file runs each held only 103 entity rows at
teardown, including the two timed creations. They do not establish performance
at an indexed corpus size of 3,800. Retain these observations as incomplete-index
diagnostics and withdraw their use, and the preceding single-pair observations'
use, as evidence of large-corpus comparative latency.

Keep the generated Markdown and timed public write/edit/read/search workflow
unchanged. Add an initial completeness gate for BOTH products, outside the
workflow clock. Observe only the current run's disposable SQLite stores through
read-only connections and one transaction per observation. Require the exact
fixture path set in the Markdown metadata table and its text-search projection:
Basic Memory `entity` plus entity rows in `search_index`, scoped to the configured
project; Exomem `pages` plus corresponding `fts` rows, with recall eligibility.
Missing, duplicate, unexpected, unindexed or wrong-project fixture identities
cannot pass. Merely seeing files on disk, a row count, or one sentinel is not a
proof. The fixture directory is exclusive to this generated corpus.

The benchmark may inspect these derived stores as pre-timing evidence; it must
not insert rows, invoke an indexer in the live service, change product defaults,
or replace any timed public call with an internal API. Keep existing public
sentinel readiness and Exomem semantic mutation admission as independent gates.
Record expected and observed membership counts/digests, proof method and schema
identity, along with full startup duration. An unsupported schema invalidates
the adapter; a missing or still-incomplete index remains pending until a separate
bounded startup deadline expires. A timeout yields an incomplete setup with no
workflow timing, never a pass over a smaller corpus. The normal per-tool timeout
continues to bound each timed public call.

Bind Exomem's store to the canonical state key derived from the requested vault,
and keep the configured state directory and selected store inside this run's
disposable root. Validate each product's supported schema version and actual
FTS5 virtual-table definition; ordinary tables with matching column names do
not prove search readiness. Close every observation's read connection. One
outer asynchronous deadline and a final pre-clock check prevent even a late
successful readiness response from admitting an expired setup.

The common workload still does not require optional graph convergence; retain
the separately labelled public-sentinel/current-graph characterization. This
setup correction does not relax the median parity target or authorize enabling
the opt-in fast-acknowledgement path only in a benchmark environment.

### Let background corpus scans yield to foreground requests

The corrected first 3,800-page pair proves complete metadata/FTS membership:
Exomem took 17,411.13 ms and Basic Memory 11,534.53 ms to verified closure.
These are preliminary individual observations, not the required paired medians.
Stack sampling during Exomem commits found both private graph construction and
the initial due-state audit scanning the corpus. Foreground CPU time was much
lower than elapsed time. A bounded pause only at graph row insertion never
executed during the measured calls; the competing work was earlier in the scan.

Two disposable prototypes retained every public operation and publication proof.
Pausing graph admission scans and due-state parsing for at most 50 ms per work
unit during canonical commands produced an 11,653.39 ms diagnostic workflow.
Covering the whole public request, including post-commit handling and retrieval,
produced 8,349.73 ms. These instrumented observations justify the amendment;
they do not replace uninstrumented acceptance runs. Neither prototype is shipped.

Add a small standard-library-only foreground-activity module. The shared
`writer_lease.invoke_command` dispatcher registers the injected vault for the
duration of the complete synchronous invocation, including reads, previews,
ordinary writes and their terminal handling. Keep its signature, routing,
authority checks, exceptions and results unchanged. Nested calls are counted
per thread and vault and unwind in `finally`. Calls without a usable injected
vault retain their existing behavior. This process-local hint is scheduling
information only: it never authorizes a read, write, acknowledgement or current
projection. Do not reuse the lease manager's global active-mutation count.

Bind activity to a canonical vault identity once at scope entry. Background
workers capture that same identity in an explicit thread-local scan scope;
ordinary foreground and synchronous maintenance paths have no background scope.
Entering a foreground invocation temporarily suppresses any background scan
scope on that thread, restoring it on exit. Thus a worker's nested foreground
call cannot inherit cooperative delays even while other requests are active.
Hot per-page checks use the bound identity and a cheap comparison with the
supplied vault spelling; they must not resolve paths, probe OS locks, inspect
SQLite, or scan other vaults on every iteration. Scope nesting restores the
previous value, and completed foreground scopes remove their counters.
On platforms supporting POSIX fork, replace this helper's mutex, thread-local
scopes and activity map in the child before it starts new work. A child must
not inherit a lock owned by a vanished parent thread or parent-only activity
and waiter callbacks. Follow the existing graph and mutation-lock reset hooks.

Enable the scope only around builders in `GraphRebuildCoordinator._run`, the
existing `schedule_background_rebuild` worker, and the due-state
`_schedule_reconcile` worker. Add cooperative checkpoints before graph recall
candidate admission and due-state page parsing, using the existing
`recall_policy.is_recall_candidate` and `find_corpus.parse_page` seams. The
checks are inert outside an explicit background scan. Do not select production
behavior by thread name, introduce another queue or delay foreground work.

At a checkpoint, another thread's foreground activity for the same vault may
cause sleeps requested in increments of at most 5 ms against a monotonic
deadline 50 ms from checkpoint entry. Request no further sleep after that
deadline; operating-system scheduling can overshoot a requested sleep.
Release the activity mutex before sleeping or invoking a waiter callback;
after the callback, resample activity under its mutex before deciding to sleep.
Test requested sleep budgets and scheduling overshoot with a fake clock and
sleeper. Resume the existing work unit when activity ends or the deadline expires; continuous
requests cannot suppress that unit indefinitely. The background thread's own
nested command must not cause it to wait for itself. Activity in another vault
does not delay it. Do not hold a new filesystem or database lock while waiting.

An explicit graph response waiter changes the priority: the registered graph
builder must bypass cooperative pauses while its coordinator has a waiter.
Check this dynamically, including during an ongoing pause, so a maintenance
request awaiting graph completion cannot slow the work it awaits. Keep existing
waiter limits, cancellation, single-flight coalescing and retry budgets intact.
The independently scheduled missing-graph worker still uses its existing
nonblocking publication path; synchronous rebuilds never acquire this scan scope.

All source admission, independent source-version proofs, generation fencing,
transaction boundaries, durable queues and recovery custody remain mandatory.
The cost trade-off is that advisory warming and background graph construction
may finish later during sustained foreground traffic. They must resume useful
work at every bounded checkpoint and converge through the existing path once
foreground activity stops. Test that completion and compare the resulting graph
and due-state projection with their ordinary synchronous results.

The graph lane owns this amendment's activity helper, dispatcher wrapper,
background scopes and focused tests, after independent critique. The root owns
the OpenSpec amendment and measurements. The same author/reviewer correction
loop applies, followed by renewed integration review. Final full-suite and
latency verification may run through the repository's existing full CI workflow
on isolated runners while the local comparison machine remains quiescent.

## Risks / Trade-offs

- A cache caption can race with a watcher event. Decline reuse on any mismatch;
  test changing freshness/configuration during acquisition and cold caches.
  Explicitly test a title edited without an event followed by a fresh direct
  key, an old direct key against a newer corpus, and unreadable non-governed
  Markdown that the broad resolver still represents by path/stem.
- Recall metadata has narrower admission than writer metadata. Use the broad
  semantic corpus only and pin excluded-namespace behavior in tests.
- A normalized dependency key can miss stem/title precedence or ambiguity.
  Use conservative lookup keys and differential full-rebuild tests for creation,
  retitle, duplicate titles/stems, deletion, rename, aliases and anchors.
- A partial dependency index could falsely prove there are no dependants.
  Source coverage, expected row counts, hashes and schema gates must fail closed;
  inject interrupted replacement and old/missing coverage in tests, including a
  positive match alongside a dependent source whose coverage is missing.
- Host load and startup work can distort comparisons. Run product workloads
  sequentially without competing task test jobs; record runtime/corpus provenance,
  individual paired observations, startup and optional convergence separately.

## Migration Plan

This changes only disposable graph-derived state. An older sidecar cannot claim
the new schema is complete; ordinary recovery rebuilds it from canonical files.
Roll back by restoring the preceding code and allowing its existing schema gate
to rebuild its compatible sidecar. Do not mutate a live cell as part of development
or delete either copy of machine-local state by hand. Open the verified change as
a ready PR; deployment and merge follow the repository's separate authority rules.
