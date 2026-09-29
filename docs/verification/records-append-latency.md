<!-- authority:non-specification -->

# Records append latency: where the time goes

Diagnosis only; no product code changed. Harness: `scripts/measure-records-append-latency.py`.
Raw numbers and cProfile output: `records-append-latency-results.json` (same directory).

## Method

- Synthetic markdown-items collection with an `item_filename` and an `item_presentation` recipe,
  invented data, seeded at 10 / 100 / 1,000 items in a temp vault with a temp state root.
- Each measurement is one `record_memory` append through the real dispatcher
  (`writer_lease.invoke_command`): idempotency ledger, mutation boundary, post-commit fan-out.
- 30 guarded appends per size, p50 / p95. Before each, the client-side guard refresh
  (`record_memory inspect`, read the container hash), timed separately. A further 30 unguarded
  appends per size isolate the guard's own cost. One cProfile pass over an append and one over the
  refresh read.
- Stage timers are inclusive wall-clock wrappers (outermost entry only), so nested stages overlap
  and do not sum.
- Limits: embeddings, CLIP and file watcher are disabled, so embedding work is unmeasured (the
  fan-out reports it as `accepted`, i.e. deferred). The vault holds only this collection, so
  costs that scale with total vault size are understated. One 4-core Linux container.

## Findings (guarded append, p50 ms)

| Stage | N=10 | N=100 | N=1000 | Grows with N? |
|---|---:|---:|---:|---|
| **Total** | 355 | 710 | 4,600 (p95 5,078) | yes, about 4.3 ms per item |
| Dispatcher outside the leaf (ledger, egress filter, digest) | 6.8 | 7.8 | 6.4 | no |
| Idempotency ledger (`ledger.run` minus leaf) | 4 | 4 | 3 | no |
| Mutation boundary enter / exit | 2.2 / 0.4 | 2.2 / 0.4 | 1.8 / 0.4 | no |
| Request validation (`_validate_values`, twice) | 1.9 | 1.8 | 1.8 | no |
| Collection resolve (3 calls) | 7.9 | 7.4 | 8.0 | no |
| Manifest load + parse (parsed 9 times) | 85 | 80 | 78 | no (fixed cost) |
| Container-hash guard: snapshot read (`MarkdownItemsAdapter.read`) | 19 | 80 | 733 | **yes, O(N)** |
| Natural-key twins + after-hash + new item path | 0.6 | 1.9 | 15 | yes, small |
| Visibility check (`require_mutation_visibility`, 2 calls) | 24 | 89 | 777 | **yes, O(N)** |
| Pre-commit authorization (contains one visibility call) | 33 | 121 | 1,031 | **yes, O(N)** |
| Item presentation render (`render_markdown_item`) | 0.8 | 0.8 | 0.8 | no |
| `exomem-item-presentation` block alone | 0.08 | 0.09 | 0.09 | no |
| `batch_atomic_write` (guard rechecks + write + fan-out) | 249 | 418 | 2,400 | **yes, O(N)** |
| Post-commit fan-out inside it (index sync, self-write registration) | 133 | 125 | 133 | no |
| of which `index.upsert_after_write` | 93 | 92 | 86 | no |
| Due-state and sweep carriers, terminal housekeeping | 3 | 3.4 | 2.9 | no |
| **Guard refresh read** (`record_memory inspect`) | 279 | 946 | 7,605 (p95 8,088) | **yes, about 7.4 ms per item** |
| **Refresh + append round trip** | 634 | 1,656 | 12,205 | yes |
| Unguarded append (no `expected_container_hash`) | 487 | 923 | 4,765 | same as guarded |

Answers to the specific questions:

- **Does the container-hash guard read or hash the whole collection?** Yes. The container hash is
  the digest of the full item inventory, so `MarkdownItemsAdapter.read` reads, hashes and
  YAML-parses every item file on each append. The guard itself is not the marginal cost: an
  unguarded append is as slow, because the same snapshot is taken regardless.
- **Index sync.** Lexical, resolver and memory-refs sync run synchronously, about 86 ms and flat.
  Embeddings are `accepted` (deferred) and the graph is `registered` (deferred). Neither grows
  with N in this harness.
- **Ledger, fast-ack, response.** Together under 10 ms and flat. Fast-ack is a closure inside the
  ledger run and has no separate timer; its cost sits inside that 4 ms.
- **File write.** The write itself is not separable from the guard rechecks inside
  `batch_atomic_write`; the fan-out is 133 ms of the 2,400 ms at N=1000.
- **Off-path.** The lease renewer and the graph rebuild thread run alongside back-to-back
  appends. Their effect on p95 was not isolated.

## Top three hot spots

**1. Per-path authorization repeats policy and state resolution for every item.**
`record_governance.py:936` (`_authorize`) calls `egress.py:5228` (`release_level_for_path_only`)
once per item per pass. Each call reloads the governance policy (`policy.py:1292`), recomputes
tombstones (`lifecycle.py:1567`) and re-derives and stats the vault state directory
(`state_paths.py:303`, `:321`, `:278`). At N=1000 an append makes about 3,200 such calls, and the
refresh read about 7,000 (roughly 70% of the refresh profile). Callers: `record_formats.py:203`
(`_require_authorized`), `record_governance.py:2784` (`precommit_authorize_mutation`), `:2812`
(`require_mutation_visibility`), `records.py:1934`.
*Proposed fix:* resolve policy, tombstone set and state directory once per operation and evaluate
each path against that resolved object (`full_release_filter` already returns a per-call filter
that could carry it). *Expected gain (estimate):* removes most of the O(N) term in both calls,
so about 4.6 s to 2 s for the append and 7.6 s to 2.5 s for the refresh at N=1000.

**2. Directory censuses and guard rechecks run about 20 times per append.**
`vault.py:3296` (`_bounded_directory_entries`) ran 21 times and took about a third of the
profile. It classifies every entry through `reserved_paths.py:1996` (`classify_logical`, about
600k regex matches). `PathGuard.recheck` (`vault.py:1143`) re-reads and re-hashes each item file
(about 12 snapshot reads per item at N=1000, `vault.py:3556`). `batch_atomic_write`
(`vault.py:4618`) is 2.4 s of the 4.6 s.
*Proposed fix:* take one census per operation and reuse it for the visibility check, read and
pre-commit; cache classification per path within a census; recheck under the held mutation lock
by stat identity (inode, size, `mtime_ns`) instead of a full re-read. *Expected gain (estimate):*
`batch_atomic_write` pre-write cost from about 2.3 s to under 0.5 s at N=1000.

**3. The refresh read snapshots the collection three times.**
`record_governance.py:1406` (`inspect_collection`) calls `record_formats.py:395` (`read`) three
times (about 19 of 22.7 profiled seconds), and `records.py:1887` (`inspect_audit_gap`) adds a
further 7 s of audit-chain reads. A client pays this between every guarded append, so it is the
largest single number: 7.6 s of the 12.2 s round trip.
*Proposed fix:* one snapshot per inspect, shared by the inspection and the audit-gap pass; and a
per-file digest and parse cache keyed by stat identity so a snapshot rebuild only reads changed
files. A cheaper option for callers that only need the guard is a hash-only read. *Expected gain
(estimate):* refresh about 7.6 s to 2.5 s from the single snapshot alone.

Fixed cost, not O(N): about 350 ms per append at N=10. The manifest is parsed 9 times
(`structured_collections.py:1061`, about 80 ms) because `records.py:2738` and
`record_governance` resolve it repeatedly; passing the parsed manifest through would save about
70 ms. Index sync adds about 90 ms.

## What this does not explain

The reported 24-row backfill took about 21 minutes, roughly 52 s per row. The measured round trip
at 1,000 items is 12 s, and the collection cap is 2,000 items. So collection size alone, in this
harness, does not reach 52 s. Candidates that this harness does not cover: a larger real vault
(index and graph work that scales with vault size), embeddings enabled, slower storage, a larger
policy set, or concurrent load from the graph rebuild. Reproducing on a copy of the affected
collection, with `call_spans` enabled on the live service, would settle it.
