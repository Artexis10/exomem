## Context

`EpistemicGraphIndex.rebuild_all()` walks every KB Markdown file and delegates each page to `_index_path()`. `_edges_for_page()` currently reacquires `find.shared_resolver()` per page. In a server with a live freshness registry that check is cheap, but a CLI or otherwise non-live process computes the vault freshness triple by walking the full vault each time. A graph rebuild over `n` pages therefore performs `n` vault walks before link resolution and becomes effectively O(n²).

Explicit reconcile ends by invalidating the live freshness maps. The watcher later calls `freshness.reconcile()` in non-seed mode; because that function currently treats an absent old map as an empty map, it reports every current Markdown file as newly changed. The watcher fans that false delta through `index_sync`, and a temporarily unverifiable embedding sidecar correctly-but-expensively records durable work for every path.

The repair must preserve graph edge semantics, conservative embedding durability, watcher recovery for real missed events, and the public reconcile contract. It must not introduce a process-global snapshot that can remain stale across operations.

## Goals / Non-Goals

**Goals:**

- Make full graph rebuild and one batched graph refresh perform a bounded, operation-constant number of resolver/freshness acquisitions.
- Keep link resolution consistent within one graph maintenance operation and fresh between separate operations.
- Make missing freshness state baseline initialization rather than evidence of source changes.
- Leave an explicit reconcile with live, exact freshness baselines when event indexes are enabled.
- Prove real post-baseline changes still dispatch while phantom corpus-wide receipts do not.

**Non-Goals:**

- Make a graph rebuild transactionally snapshot concurrent filesystem changes.
- Change wikilink resolution, graph schemas, edge types, embedding freshness classification, or durable receipt semantics.
- Repair the separate vault audit findings or inherited graph-semantic/Windows-newline baseline failures.
- Add an event journal or new persistence layer.

## Decisions

### Use one detached resolver per graph maintenance pass

`rebuild_all()` and `refresh_paths()` will acquire one detached resolver snapshot before opening or mutating the graph sidecar, then pass it explicitly through `_index_path(..., resolver=...)` to `_edges_for_page(..., resolver=...)`. `_edges_for_page` keeps an optional fallback for its existing direct callers, while graph maintenance always supplies the snapshot.

This makes resolver/freshness acquisition O(1) per operation and page parsing O(n). A detached resolver cannot be changed midway by a concurrent watcher patch. Resolver acquisition occurs before graph deletion so acquisition failure preserves the existing sidecar. Separate `refresh_paths()` calls reacquire freshness, while one multi-path call shares one snapshot.

### Stabilize full rebuilds against concurrent target changes

A full rebuild brackets each resolver-plus-graph pass with direct disk-truth
vault freshness walks. The disk key is supplied to detached resolver acquisition,
so a stale event-maintained registry cannot cause a stale shared resolver fork.
If the key moves during the first pass, the rebuild retries once with a newly
acquired snapshot. A stable vault therefore retains one resolver acquisition and
one O(n) graph pass; a moving vault is capped at two acquisitions and two O(n)
passes, never per-page O(n) freshness work.

If the disk key moves during the second pass, the rebuild deletes its graph
schema-version marker and raises. The resulting sidecar is explicitly
unavailable/non-current, so readers cannot trust the unstable graph and a later
refresh or reconcile rebuilds it. The first attempt still acquires its resolver
before any graph mutation.

The schema-version marker has one publisher: stable full-rebuild completion.
Pass setup removes it in the same transaction that clears prior rows, and it
stays absent throughout page indexing and any bounded retry. `_index_path()`
updates graph rows and registry/profile metadata but never graph availability,
so an incremental refresh already admitted against the old current graph cannot
resurrect a failed overlapping rebuild. Refresh against a missing or unavailable
sidecar routes through a full rebuild rather than publishing a partial graph.

All public graph mutators (`rebuild_all`, `refresh_paths`, and `delete_paths`)
share one `VaultMutationCoordinator` boundary. Its state root is
`<Knowledge Base>/.graph-coordination`, keyed by the canonical vault identity,
with a 30-second bounded acquisition timeout. Keeping the OS-backed lock inside
the vault is intentional: the LocalSystem service and an interactive user have
different per-account runtime/cache roots, but both see the same vault path and
therefore the same lock file. The coordinator's process-local `RLock` preserves
same-thread re-entrancy; refresh routes to a locked rebuild internal while still
holding the outer boundary.

The lock covers availability decisions, resolver acquisition, graph row and
metadata changes, disk-truth stabilization checks, and final marker publication.
An older operation therefore cannot publish or remove availability after a
newer mutation has begun, and a refresh admitted before a rebuild cannot mutate
the sidecar during that rebuild.

`.graph-coordination` is excluded by both the KB corpus walker and the full
vault walker. Lock state is not corpus content, and excluding the subtree before
recursion prevents lock-file ACL/read behavior from entering freshness or
resolver scans. Structured lock failures (`MUTATION_BUSY` and
`MUTATION_LOCK_UNAVAILABLE`) propagate through the graph leaf wrapper to
`index_sync`, which records that component as degraded while allowing the
canonical Markdown write and other index lanes to complete. Unstructured graph
leaf failures retain the existing soft-failure behavior.

Failure ordering follows the same mutation boundary. An initial disk-freshness
or resolver acquisition failure occurs before the first pass and preserves the
previously current graph. Once a pass starts, any exceptional exit—including
partial indexing, the post-pass freshness check, or acquisition for a required
retry—leaves the schema-version marker absent before propagating. A partial or
known-moved pass can therefore never remain advertised as current.

Alternatives rejected:

- Reusing only the global shared resolver would still require freshness validation at the wrong per-page boundary and would allow concurrent mutation during a rebuild.
- Passing one freshness tuple through repeated shared-cache lookups removes the walk but retains unnecessary global coupling and repeated acquisition calls.
- Bulk-compiling a separate link table could reduce constants further but is a larger redesign with no evidence it is needed after restoring linear scaling.

### Distinguish an absent baseline from a seeded empty baseline

`freshness.reconcile()` will atomically install the fresh map in both cases, but when no prior map exists it will return an empty, non-drift delta. A prior map of `{}` remains a real baseline: files that appear afterward are reported as changes. Each scope is handled independently.

This is the semantic boundary that prevents initialization from masquerading as missed filesystem events. It does not weaken recovery: once a baseline exists, create/modify/delete deltas continue to dispatch exactly as before.

### Rebaseline explicit reconcile immediately

Successful write-mode reconcile will seed exact `kb` and `vault` freshness maps from final on-disk state before returning, rather than invalidate them and wait up to one watcher interval. The inbound-link cache is still cleared so its next read rebuilds from final disk state. Existing resolver cache entries remain safe because their stored triple is compared with the new baseline and rebuilt once if it changed.

If a final baseline scan cannot complete, reconcile will log the failure and leave that scope non-live; the next periodic watcher pass installs a baseline without fanout. Event-index kill-switch behavior remains unchanged.

`freshness.seed()` intentionally consumes the final stat-entry iterable while
holding the registry lock. Scanning outside the lock and then overwriting the map
would allow an event patch published after the scan to be lost by the later
install. The bounded explicit-reconcile scan may briefly delay registry readers,
but preserves exact race ordering; this change does not trade correctness for a
shorter lock hold.

### Test structural work, parity, and fanout—not a fragile stopwatch

Scaling tests count detached resolver acquisition and freshness/vault-walk boundaries across many pages. Parity tests compare graph results for representative and ambiguous links. Stabilization tests deterministically change a target after snapshot acquisition and force repeated churn to prove the retry cap and unavailable failure state. Freshness/watcher tests cover missing versus empty baselines, independent scopes, no phantom index dispatch or receipts, and exact-once modification and deletion recovery. A quiescent real-vault benchmark is retained as deployment evidence, not as a timing assertion in lean CI.

## Risks / Trade-offs

- [Risk] Files change during a detached graph rebuild. → Disk-truth bracketing retries once with a fresh snapshot. Repeated churn marks the graph unavailable and raises, leaving later watcher/reconcile maintenance to rebuild instead of trusting stale edges.
- [Risk] Rebaseline scan races a filesystem event. → Event publishers continue patching live maps and periodic reconciliation remains the bounded safety net; missing state is never converted into fabricated drift.
- [Risk] A broad test run is not green on current Windows `main`. → Pin new acceptance to targeted regressions, record inherited failures, and require CI/independent review to show the PR adds no new failures.
- [Risk] A cold detached resolver still performs a full vault read. → One O(n) read is required for correct resolution; the contract removes the repeated per-page O(n) read/check.

## Migration Plan

No data migration is required. Deploy the code, run targeted and lean verification, then run explicit reconcile once on a quiescent production vault while measuring wall time, queue counts, graph drift, and health. Rollback is a normal code rollback; graph and freshness state are derived and rebuildable.

## Open Questions

None.
