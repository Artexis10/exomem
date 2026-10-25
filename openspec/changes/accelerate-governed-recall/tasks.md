## 0. Intake And Baseline

- [ ] 0.1 Record the baseline on the live cell before any code lands: thirty novel hybrid recalls, thirty keyword recalls and thirty `projects`-filtered hybrid recalls over the direct transport with timing diagnostics, load average at or below 2.0, p50/p95 per series and per-stage medians; store the content-free artifact under the change and verify the two walk stages (`filter_eligibility`, `outside_kb`) are present in the filtered series.
- [x] 0.2 Inventory every supported structured-filter field and record, per field, whether today's plan classifies it complete or unsupported and which index (semantic-unit sidecar, lexical catalogue, media sidecar) can answer it; verify the inventory lists every field the registry accepts.
- [x] 0.3 Prepare one delegation packet per lane (1–5) from this change with base SHA, allowlist, red-first nodes, mutant matrix, exact gates with out-of-tree state roots and basetemp, and a fresh reviewer; verify every gate line executes once before dispatch.

## 1. Lane 1 — Walk Sentinel And Timing Completeness

- [x] 1.1 Add red tests: a walk sentinel that counts directory enumeration of the knowledge-base scope during one real `find` and fails when it is non-zero, and an attribution test that drives the public leaf with timing enabled and asserts `sum(stage.ms) + unattributed_ms <= total_ms` and `unattributed_ms <= 0.15 * total_ms`; verify both fail on the current tree for a `projects`-filtered hybrid recall.
- [x] 1.2 Make the stages table write-through from registered spans, add the per-stage `source` value, and span every remaining manual timing region named in the 2026-08-31 plan (`_find_semantic`, admission folded into `recall_projection`, the unit lane embed, the deep copies); verify the attribution test passes and the diagnostics carry a source for every stage.
- [x] 1.3 Mutation proofs in a harness-owned copy: restore one manual timing write and require the attribution test red; remove the sentinel installation and require the walk test red; restore and verify green, Ruff and `git diff --check` clean.
- [x] 1.4 Author-independent reviewer attacks the sentinel (can a walk hide behind a cached listing or a subprocess), the bounds, and the source vocabulary; apply corrections and require recheck before acceptance.

## 2. Lane 2 — Index-Backed Structured-Filter Eligibility

- [x] 2.1 Add red tests: a `projects` filter, a `tags` filter, a `types` filter and an AND of a page clause with a unit clause each resolve with the walk sentinel at zero and return the same path set as the scan oracle on a fixture vault; a stale catalogue generation yields the typed warming outcome; a pending write is evaluated against its committed frontmatter; verify they fail before implementation.
- [x] 2.2 Implement the page-metadata index in the lexical catalogue store (path, filterable frontmatter fields, content hash, generation), written by the existing catalogue fan-out component under the write receipt and rebuilt by the single-flight repair; extend `plan_index_candidates` to page clauses; route eligibility through the index for managed readers and keep the oracle for offline mode; verify `tests/test_structured_filters.py`, `tests/test_find_filters*.py` and the new tests pass with an out-of-tree state root.
- [x] 2.3 Add an identity test that evaluates every filter in the inventory from 0.2 against both the index and the oracle on the fixture vault and requires equal sets; verify it passes and is model-free.
- [x] 2.4 Mutation proofs: drop one field from the index writer and require the identity test red; answer a stale generation from the index and require the warming test red; restore and verify green.
- [x] 2.5 Reviewer attacks correctness of `$in`, `$exists`, `NOT` and OR composition against the oracle, generation binding, and the pending overlay; apply corrections and require recheck.

## 3. Lane 3 — Exact Custody For Read-Side Caches

- [x] 3.1 Add red tests: after one governed write the lexical corpus, eligibility catalogue and frontmatter cache report an exact update of that page's rows only (no rebuild counter increment); a receipt-less external edit still invalidates the scope on reconciliation; a burst of writes to distinct pages leaves every cache row equal to the page's current frontmatter; verify they fail before implementation.
- [x] 3.2 Implement per-path invalidation seams on the substrate caches and apply each committed receipt's path set to them; keep whole-scope invalidation for receipt-less drift; verify the tests pass and `tests/test_read_after_write_visibility.py`, `tests/test_pending_recall_delta.py` and `tests/test_lexical_deferred_upsert.py` stay green.
- [x] 3.3 Add an audit that compares cache rows against frontmatter after a burst and fails closed to a scope invalidation on any mismatch; verify the audit test passes and a seeded mismatch is caught.
- [x] 3.4 Mutation proofs: skip a moved path's old row and require the burst test red; let a whole-scope key evict a receipt-covered cache and require the exact-update test red; restore and verify green.
- [x] 3.5 Reviewer attacks moves, deletes, restart hydration, and an in-flight graph rebuild's ability to invalidate read caches; apply corrections and require recheck.

## 4. Lane 4 — Opt-In Widening And Surface Regeneration

- [x] 4.1 Add red tests: default `scope="kb"` runs no widening stage and reports it skipped; the widening option runs one catalogue query over the index-resolved out-of-KB set with the sentinel at zero; a stale catalogue declines; the reserve never exceeds `limit - 1`; verify they fail before implementation.
- [x] 4.2 Add the widening option to the `find` leaf and `ask_memory` (default off), implement the catalogue-backed reserve, and regenerate the MCP schema fixtures, the tool-surface contract, the hosted candidates and both Claude directory channels, the ChatGPT pending digest and the v1 release identities; verify `tests/test_mcp_schema_fidelity.py`, `tests/test_connector_guardrails.py`, `tests/test_hosted_epistemic_profile.py`, `tests/test_hosted_marketplace_release.py`, `tests/test_hosted_plugin_rendering.py` and `tests/test_lazy_imports.py` pass.
- [x] 4.3 Mutation proofs: re-enable widening by default and require the default test red; let a stale catalogue fall through to the scan and require the decline test red; restore and verify green.
- [x] 4.4 Reviewer attacks the surface change, the reserve cap, and the documentation of the behaviour change; apply corrections and require recheck.

## 5. Lane 5 — Integration, Gate And Live Evidence

- [x] 5.1 Merge the accepted lanes into one integration worktree, rerun each lane's focused gate, and verify the aggregate diff stays inside the union of the lane allowlists.
- [x] 5.2 Add `scripts/recall_latency_gate.py` with the three series from the contract, novel queries, warm caches, quiescence refusal above load 2.0, load recorded per percentile, content-free output, and `--check` against the fixed ceilings; add `tests/test_recall_latency_gate.py` pinning the ceilings, the refusal, and the series shape; verify the tests pass.
- [ ] 5.3 Run the full guard→mutant→test matrix of lanes 1–4 against the aggregate source copy and verify every node turns red under its mutant and green after restore; preserve the matrix results.
- [ ] 5.4 Run the full corpus on the merged tree with an out-of-tree basetemp and `--timeout=300`, Ruff, the privacy gate, `git diff --check` and `openspec validate --all --strict`; record commands, exit codes and SHAs, and attribute every failure.
- [ ] 5.5 Fresh integration reviewer over the aggregate diff with adversarial probes for result identity, stale-answer paths, and the surface change; apply corrections and require recheck.
- [ ] 5.6 After release and upgrade of the live cell, run the gate on a quiescent cell and record before/after against 0.1 and the 2026-10-09 reproduction in `baseline.md`: every series against the ceilings, the targets and the stage budgets of `recall-latency-contract`, zero walks, the owner and admitted series apart. Add the live-cell series to `docs/release.md` under "Managed Linux service: what to check after the handoff", after `/health/ready` reports `ready`: the agent or operator who rolls the cell runs it and attaches the content-free summary to the release's GitHub Release with `gh release upload "$TAG" recall-latency-live-cell-<UTC time>.json`. Record the state as passed, failed, refused or not measured. The state gates nothing; a failed or refused state opens a GitHub issue that names the release, the series and the stages over budget. Verify the artifact is stored under the change and attached to the release.
- [ ] 5.7 After section 6, update task checkboxes from evidence, sync delta specs into the canonical specs, run `openspec validate --all --strict`, archive with `openspec archive accelerate-governed-recall`, and validate again.
- [ ] 5.8 Before the archive in 5.7, refresh every MODIFIED block of tranches 1–5 (`structured-retrieval-filters`, `find-recall-efficiency`, `recall-read-path`) against the canonical specs as they are then, keeping requirements and scenarios that later changes added; verify `openspec validate --all --strict` passes and the archive removes nothing a later change added.

## 6. Subsecond Warm Search

The slices follow the gate, so each one reports a before and an after on the same instrument. Task numbers stay as the first review cited them, so the order of work is: 6.1, 6.2, 6.10, 6.3, 6.4, 6.14, 6.5, 6.12, 6.6, 6.7, 6.8, 6.11, 6.13, 6.9. After the CPU spin slice, the order follows the p50 saving measured on a default hybrid request (run R3 in `baseline.md`, p50 / p95); 6.6 is placed by its saving on `mixed` requests. 6.14 saves nothing on a default hybrid request, and no run measured a keyword-recall series, so it follows 6.4 by dependency: it edits `_substring_query`, whose plan 6.3 verifies, and runs on 6.4's catalogue session. 6.13 lands last before the close, because it needs 6.4's catalogue session, 6.6's unit lanes and 6.10's thread policy (design decision 10). Slices 6.3–6.8 and 6.14 land after #1649, #1645 and `fix/admission-candidate-sizing` (design decision 10). Each slice keeps results identical, proves it with an identity test on the reference corpus, and gets an author-independent review.

- [ ] 6.1 Gate first: extend `scripts/synth_vault.py` with the reference corpus and query mix the contract names. Extend `scripts/recall_latency_gate.py`'s in-process transport to:
  - pin two CPUs, load the served encoder, and seed corpus vectors through the index build;
  - span the query encode as `vector.embed` wherever it first runs, inside `semantic_units` included, and report span keys that no stage row counts as "unbudgeted";
  - run as the vault owner through the product's owner-authority path with nothing patched, plus a separate admitted-principal series;
  - run the hybrid, keyword, filtered, dedicated temporal and `mixed` series, clear only the result cache between samples, and send query text unchanged;
  - check quiescence once, before the series, and record the load beside every percentile; on the live cell, have the served process record each request thread's run-queue delay from `/proc/self/task/<tid>/schedstat` in the timing diagnostics, apply the contract's contended-sample rule, and report the contended count and the p95 with contended samples included;
  - time the entire `ask_memory` call, including response work after the retrieval snapshot; record wall time and process-clock `cpu_ms` per stage, plus `overlap_cpu_ms` and `unattributed_cpu_ms` per request, as the contract states; report every CPU field as unknown on a platform without `/proc/self/task` or the process clock, and a stage with fewer than 20 samples as insufficient;
  - before any budget is trusted, measure the query encode for each query-length band (1-3, 4-9 and 15-40 words) on one and on two intra-op threads, on the pinned profile, and store the figures under `verification/`;
  - ground the BM25 and keyword targets with a replay on a new connection per request, on SQLite 3.45.1 and 3.46.1, over the whole query mix with the 15-40 word band included, and with keyword recall's page reads and hit construction inside the keyword row; until then report both stages against 8 ms and apply no twice-budget rule to them;
  - record the SQLite version in every summary, and give no ceiling verdict for a workstation run on a SQLite outside the reference; keep the reference versions (today 3.45.1 in CI's system Python, 3.46.1 on Cloud cells) with the gate's other inputs under `benchmarks/recall-latency/`, so that a SQLite upgrade in CI or the Cloud image is a one-line data change;
  - in absolute mode, check the ceilings (200 ms p95 for every warm hybrid series, 100 ms p95 for keyword recall) and unknown samples; report each hybrid p95 beside the 100 ms target, the keyword p95 beside the 50 ms target and each p50 beside the 50 ms target, and record each target as met or missed; report each stage row against its budget, failing a row above twice its budget without failing the series; record the encode's measured floor when its p95 exceeds its budget; and write the content-free summary with the state that 5.6 and 6.9 record: passed, failed, refused or not measured; the state gates nothing;
  - in paired mode, compare head with the most recent release tag in the `retrieval-latency` job (10% margin, 95% interval), report a stage that the tag does not span as "not comparable", and cache the encoder artifact by digest;
  - load each arm's runtime dependencies from its source lock, prepare an isolated frozen tag environment, and record lock digests and environment policies; refuse a failed tag preparation without substituting head's dependencies;
  - use one private measurement adapter for both arms, preserving their actual collectors, authority paths and public diagnostic suppression; pause only the inactive owned benchmark worker outside sample timers, and resume it during cleanup;
  - conserve process CPU across row, overlap and unattributed buckets, including excluded waits; support explicit fork/join branch lifetimes, declare serial execution, and report unknown paths without inventing membership; verify internal gaps and shared setup/response work;
  - read the committed accept record `benchmarks/recall-latency/accept.json` (tag, series, maximum ratio, the full-CI run that measured the slowdown, reason, pull request): report a covered series within its maximum ratio as "accepted", fail an uncovered or exceeded series, report a record for an older tag as expired, and validate the record's shape;
  - add a `docs/release.md` step after the tag: the agent or operator who drives the release deletes an expired accept record in a pull request, unless the next accept record's pull request replaces it first;
  - validate the live-cell summary against its content-free schema before the runbook step uploads it.

  Update the pins in `tests/test_recall_latency_gate.py`. Verify that an absolute run of main on a quiet workstation reproduces the `baseline.md` shape and fails, that a paired run of main against itself passes, that a seeded 30% slowdown in head fails the paired run, and that the same slowdown with a matching accept record reports accepted, and that a record for another series or a lower ratio still fails. Verify that a record for an older tag is ignored, reported as expired, and no longer excuses the slowdown, and that a workstation run on SQLite 3.53 gives no ceiling verdict.
- [ ] 6.2 Structural pull-request gate: a model-free test on a 400-page generated corpus and on the same corpus grown to 1,600 pages with filler whose vocabulary and links are disjoint from the query list (design decision 9). It drives `ask_memory` and `activate_context` as the owner and as one admitted principal, and asserts the same matched rows and candidates at both sizes. It counts SQLite virtual-machine steps per timing span, connections per store and scope, readiness proofs and Markdown page reads, and checks step growth only as ratios between the sizes. Register it in the pull-request tier in `tests/test_ci_reliability_contract.py`. Run it on SQLite 3.45.1, the older reference SQLite, from CI's system Python 3.12.3, as well as on the pull-request tier's own SQLite, and name each version in its report. Include a keyword-recall query that matches more pages than its limit. Verify that on the pre-fix baseline it fails on the step ratios of the parent-hint query and of `_eligibility_metadata_query`, on the ranking stages' Markdown reads, and on that keyword recall's Markdown reads, not on BM25 or keyword step ratios; record the ratios, never absolute counts.
- [ ] 6.3 Keep parent-hint and eligibility lookups candidate-driven, verify the keyword plan, and record `parent_hints` separately.

  The baseline parent-hint p50/p95 was 84.7/132.3 ms; its 2 ms target remains unverified on the installed release.
  Parent-hint and eligibility queries now use candidate-driven joins.
  The keyword `MATCH` plan already drives `pages` from `tri` on SQLite 3.45.1 and 3.50.4.
  These plan checks establish no installed latency gain.

  1. Preserve `FROM json_each(?) r CROSS JOIN pages p ON p.path = r.value` for parent hints.
  2. Drive eligibility from `wanted` with `FROM wanted w CROSS JOIN pages p ON p.path = w.path`, preserving requested parents.
  3. Keep the verified `tri JOIN pages` MATCH plan and the intentional short-token scan.
  4. Record executed parent-hint lookups in `parent_hints`, including their source; omit the span when candidates are empty.
  5. Check plans on SQLite 3.45.1 and local SQLite; record both versions and the `wanted` parent lookup.
  6. Verify identical rows, including parents, and structural step ratios for both candidate queries.
- [ ] 6.4 One catalogue read session per selection: prove readiness against the selection's checkpoint and reuse its connection. Verify identical results, one connection and one full proof per store and scope, and invalidation after a registry edit without note changes.

  Preserve the registry loader's existing parser and digest cache with fresh binding, admission and path checks. Measure the remaining serialization and hashing before adding a pure memo; never retain a loaded basis or skip its checks.

  The baseline BM25 p50/p95 was 29.2/55.8 ms, and keyword was 39.4/90.6 ms, with nine connections and six proofs. The 8 ms lexical targets remain unverified.

  A cache miss can use a probe selection and another after encoding; count each once. Activation also separates initial lexical evidence from post-encode compilation. Both phases remain optional and defer lag repair until activation ends.

  Selections follow `keep-recall-available-during-pending-recovery` (2.4, 2.5): ordinary publication preserves admitted rows, membership and statistics; policy or semantic-identity changes still refuse. Independent readers keep their own proof, and a missing checkpoint keeps its refusal.

- [ ] 6.5 Ranking metadata from catalogue columns (fusion and multipliers 24.4 / 65.6 ms, temporal 43.6 / 124.4 ms, graph 26.3 / 135.4 ms, 92 hydrations and 126 page file reads per request; expected at or below 5, 3 and 9 ms): read type, status, `updated` and emitted parent from `pages` for the request's generation; add derived catalogue columns for the lexical guard's per-page `stem_set` and `letter_script` (`find_policy.py:754-809`); answer the graph seeds' `make_excerpt` and `stem_tokens_present` test (`find_candidates.py:702-708`) from catalogue text; hydrate Markdown only for returned hits; verify fused-order identity, identical guard decisions and graph seeds, and Markdown reads within the candidate count plus the fixed overfetch.
- [ ] 6.6 Unit lanes (`mixed` and `unit` levels, baseline 647.4 / 2,142.9 ms and 442 page reads per request; expected at or below 12 ms): preserve the shipped generation-bound matrix, admitted lexical statistics and selected parent reads. Use thread-qualified measurements to profile the remaining admission cost before adding reuse. Verify unit-hit identity for the owner and an admitted principal, with mixed Markdown reads within the candidate count plus fixed overfetch.

  Repair finite mixed completeness before qualifying the paired latency gate.
  Defer only the exhausted candidate-window verdict until relation survivors,
  parent capping and the finite mixed plan are known. Clear that verdict only
  when enough capped units survive and every frozen represented parent has a
  complete admitted structural upper bound at or below the existing cap.
  Include page-only parents and frozen pending summaries. Missing occurrence
  rows cannot prove a smaller bound. Larger or unproved parents retain incomplete
  status; do not report their prefix counts as exact positive truncation totals.
  Preserve exhaustive structured acquisition, admitted BM25 statistics, exact
  counts for complete acquisition, frozen annotation targets and no refill.
  Verify small and large parents, missing rows, pending and page-only parents,
  relation filtering, structured filters and selected evidence loss.

  The released baseline also has the exhausted-window defect. Deliver this
  product prerequisite in a containing release before the instrument qualifies
  against the latest tag. The remaining admission optimization joins this batch
  only after its dependency and trust design settle. These observations establish
  neither the unit-stage budget nor full task completion.

- [ ] 6.7 Dense chunk text after fusion (dense search 33.1 / 52.2 ms, of which chunk text 14.7 / 35.2 ms; expected at or below 15 ms): carry modern chunk references through fusion and frame collapse, which moves references without reading text. Hydrate only the frozen selected prefix that consumes excerpts, transcript localization and nearest-frame annotations, or rerank passages. Bind each reference to its actual scored cache or transaction, source index, space and publication token. Reuse the chunk-path log and primary-key text lookup in one short proof-and-text transaction. On proof loss, omit selected evidence with incomplete coverage and no cache, replacement passage or lower-ranked refill. Preserve empty-text fallback, first-chunk tie behavior and migration source ownership. Reuse the existing private source-hash map for emitted, chunk and frame pages. Verify unchanged-evidence identity, selected text read bounds, unrelated-publication reuse, changed-path refusal, cache-swap and vec-transaction races. Generation-zero NumPy keeps its existing eager path; it remains unoptimized and unattested, so this task stays open for that exception and the full acceptance proof.
- [ ] 6.8 Response blocks inside the timings (hydration and response 29.8 / 78.9 ms; the due-state block's 20.7 / 58.1 ms is outside `total_ms`; expected at or below 8 ms for hydration and 3 ms for `due_state`): register the due-state block as the stage `due_state` and read role state once per request; verify that caller elapsed time exceeds `total_ms` by at most 10 ms at p95 and that response content is unchanged.
- [ ] 6.9 Close: run the absolute verdict on a quiet workstation and the live-cell series, and record a before and after for every stage against `baseline.md`, with the owner and admitted series apart. The workstation run links a reference SQLite. Re-measure the query encode on the pinned profile; if its p95 alone exceeds the 40 ms budget, record that p95 as the measured floor, and move neither the ceiling, the target nor the budget. The change completes when the quiet-workstation verdict against the 200 ms ceiling (100 ms for keyword recall) on the reference corpus is recorded as passed, with its summary stored under `verification/`. A failed ceiling keeps the change open. A refused or not-measured state completes nothing. The summary also records the target outcome for each series, met or missed, with the measured encode floor and the failed stage rows; a missed target never blocks completion (design decision 9). Record the live-cell state as 5.6 does; a failed or refused state opens follow-up work and does not hold the change.
- [ ] 6.10 Process CPU beside each request (CPU over wall 1.89 at p50 in R3 with the encoder loaded, 0.42 in model-free M1; design decision 11): attribute the spin with `cpu_ms` per span, then set the cell thread policy, covering ONNX intra-op spinning (`session.intra_op.allow_spinning`), OpenBLAS idle waiting, thread counts and `session.disable_prepacking` on cells that share weights; verify identical encoder output, no stage other than `vector.embed`, `vector.search` and `semantic_units` with `cpu_ms` above its wall time, and no encode slowdown on the paired comparison.
- [ ] 6.11 Request setup (measured 9.4 / 28.1 ms against 4 ms: `recall_projection` 6.0 / 14.1, `pending_visibility` 2.2 / 4.5, `freshness` 1.1 / 2.7): profile the three spans, remove the per-request work that the profile names, and verify identical projection, freshness and pending-visibility outcomes and the row against its budget.
- [ ] 6.12 Graph expand and resolver (measured `graph.expand` 13.9 / 31.4 ms and `graph.resolver` 5.1 / 16.3 ms; the lane's budget is 9 ms once 6.5 moves the seeds): profile both spans, remove the per-request work that the profile names, and verify identical expansions and graph-lane order and the lane against its budget.
- [ ] 6.13 Query encode beside the lexical lanes (design decision 12; run one after another, the budgets sum to 133 ms, and a default hybrid request to 102 ms): after eligibility, run the encode branch (query encode, then dense search) and the lexical branch (BM25, keyword, CLIP where it is enabled, and the unit lanes, whose vector search waits for the query vector) at the same time, and start the stages after the lanes when both end. On the concurrent path, run the served encoder on one intra-op thread on a two-CPU cell. Give each branch its own connections, the lexical branch owning 6.4's catalogue session, and keep lane statuses, the degraded and failed lists and the retrieval trace in the serial order. Record each span's thread and explicit fork/join branch contexts, including gaps and the join; span the time that `semantic_units` blocks on the query vector as `vector.wait` and exclude it from the unit-lane row; count a row's wall time as the union of its intervals and overlapping intervals once wherever they are added up; record each request's critical path; report `cpu_ms` outside the overlap only, plus `overlap_cpu_ms` and `unattributed_cpu_ms` per request, as the contract states. Update `tests/test_read_path_timing_attribution.py:176-183`, which asserts that the sum of the undotted stages is close to the covered time, to check the covered time against the union of the root-level intervals, overlapping intervals counted once, instead of their sum. Run the reference series serial and concurrent on the paired cases. Keep the concurrent path only when it helps: the upper bound of the 95% interval of the hybrid series' paired concurrent-to-serial ratios is below 1.0, and the concurrent encode p95 stays within 10% of a serial encode on one intra-op thread. If concurrency does not help on two CPUs, record that finding with both arms' figures, keep the serial path, and complete the slice with its finding. Verify:
  - identical fused order, hits, excerpts and lane statuses against the serial path on the reference corpus;
  - the keep or serial decision and its figures, recorded under `verification/`;
  - one row per stage in the timing table, with wall time, `cpu_ms` and the share of requests on the critical path, no row counting `vector.wait`, and the attribution bounds holding with the branches overlapped;
  - a before and an after of every series on the pinned profile, with each hybrid series' p95 judged against the 200 ms ceiling and reported against the 100 ms target.
- [ ] 6.14 Keyword recall's page reads (design decision 8): order and limit emitted groups in SQL before reading Markdown. Reuse 6.4's catalogue session and the existing pending snapshots:
  - merge all frozen pending parent metadata before grouping, including creates, edits and tombstones;
  - determine grouping from admitted parseable metadata, preserving existing raw-candidate and referenced-parent authorization;
  - apply emitted-page filters before the group cap and order by the emitted page's catalogue date and path, descending;
  - retain the excerpt and annotation sources needed for the latency contract's existing frame precedence;
  - hydrate only selected groups, validate all disclosed source hashes, and preserve incomplete-result cache exclusion after canonical drift;
  - carry private source hashes through the existing cache and validate selected child evidence on cache hits.

  Verify unchanged-evidence parity against the exhaustive path for hits, order, excerpts and frame annotations. Cover pending parent changes before the cap, invalid and out-of-scope parents, denied referenced parents, separate excerpt and annotation sources, and selected drift without promotion or refill. Verify the amended selected-source read bound and corpus-growth ratios through the existing structural gate. The latency-contract delta records the canonical-drift difference; this task stays open until its implementation and proof complete.

### Task 6.1: source-loaded bar reconciliation

The existing runner now loads the current hybrid and keyword ceilings, diagnostic
targets and reference premise from `request_latency_contract.json`. It reports
unknown timings separately from typed warming and labels full acceptance as
not measured. Later process load remains diagnostic after initial quiescence.
Missed targets and stage budgets diagnose their rows without failing a ceiling.

Task 6.1 remains open. The source runner now defines the reference corpus and
query mix, includes principal and encoder checks, and records interval unions
and live run-queue attribution. Hosted execution must verify stored rows,
worker execution, paired qualification and the complete job duration.
Encoder thread profiling, SQL replay and absolute and live acceptance remain
unmeasured. Model-free transport checks establish no live acceptance.
