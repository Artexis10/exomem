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
