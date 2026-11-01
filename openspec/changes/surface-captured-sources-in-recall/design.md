# Design

## Context

See `proposal.md`. Context activation excludes raw material in every carry path. `working_set_runtime._is_raw_material` filters carry results, and `lexstore.search_bm25_result(exclude_raw_material=True)` filters them in SQL. The activation index never makes an anchor of a page in `Sources/` or `Evidence/`. Request admission (`lexstore._AdmittedRecallRows.carry_candidates`) also omits raw material, so a governed vault cannot rank a captured page under the caller's statistics. In the default `maps` packet, `context_views._slice_rows` keeps only leads that have a context-catalogue row, and that catalogue omits raw material except Episodes.

On 2026-11-01, a live probe asked for an invoice that only a captured page records. Ordinary search ranked that page second. Context activation returned an empty packet.

## Goals / Non-Goals

**Goals:** a captured document that a turn names reaches the packet; compiled notes keep precedence; the release guard decides captured material exactly as it decides other material; one value restores the previous packet.

**Non-Goals:** carrying captured body text; making captured pages anchors; compiling the backlog; changing the compiled carry's outcomes; recalling old Episodes by phrase.

## Decisions

### D1. Same phrase gate, separate pass after the compiled carry

`carry_named_groups` returns, through an optional collector, the phrases it judged: component pairs, rare tokens and compiled survivors. A new pass queries the captured pool once per phrase with the same corroboration, term budget, checkpoint and `allow_delta=False`. Its `allowed_paths` is the captured pool plus that phrase's compiled survivors, so both pools score under one statistic. The pass has its own budget stop. Exhausting the budget drops only captured pointers.

Adding captured pages to the compiled query instead would change the compiled result window. It would also mark phrases `contested`, because a compiled note shares its Source's words. Compiled outcomes would then change.

### D2. Compiled precedence reuses the recall factor

Each score is multiplied by `find_policy.page_type_factor`, the helper that recall uses for the note-type factor and the media exemption. A captured page qualifies when its raw score clears `RETRIEVAL_CARRY_MIN_SCORE` and its adjusted score beats the best adjusted compiled survivor of the same phrase. The change adds no ranking constant.

### D3. Pointers, not units

Captured material leaves as `pointers[]` entries, because the egress guard already decides pointer `ref` and `anchor` paths. A new packet block would be a new egress surface. Body text would need its own egress review, and the agent can read the page through `read_memory`.

### D4. The bound is service configuration

`EXOMEM_ACTIVATION_SOURCE_SLOTS` (default 2) sits beside the other activation settings in `working_set_runtime`. It is part of the packet cache key and is documented in `docs/deployment.md`. Activation reads the shipped ranking configuration, so a `RankingConfig` field could change only with a deploy. At 0 no collector is passed and no captured query runs.

### D5. Admission gains a captured partition

`RequestQueryContext` takes a flag that also admits the raw-material partition, minus Episodes, through the same release object. The flag is part of the admission generation key. Without this change, a governed vault would either rank no captured page or rank one on statistics the caller may not use.

### D6. Episodes stay out

Episodes reach the packet as recent context and threads. The pool excludes them in SQL with the recent-context block's own Episode predicate. Excluding them in Python instead would let thousands of Episodes fill every window before the filter ran.

### D7. Lineage attaches cited pages

For each carried or resolved compiled page, `source_closure.claims_in` and `resolve_reference` identify the captured pages it cites, as `source_coverage` already does. A cited page becomes a pointer anchored on the citing note and gets no slice lead.

## Risks / Trade-offs

- **Bulk folders.** Term statistics count compiled pages, so a word rare there can be common among captured pages. The window-full refusal, the Episode exclusion, the bound and charging captured pointers last together contain flooding.
- **First anchorless served packet.** Readers that assume at least one anchor need checking: hook overhead accounting, the sensed model, due-state association and the call ledger.
- **Latency.** Admission now covers about 3,000 more pages per snapshot generation. The real-vault acceptance run compares activation latency between the two trees.
- **Concurrent work.** Open PR #2237 rewrites admitted statistics. Whichever change lands second rebases and reruns the latency comparison.

## Migration Plan

The change rewrites no stored data. It ships through the ordinary release. To undo it without a deploy, set `EXOMEM_ACTIVATION_SOURCE_SLOTS=0` and restart the service.
