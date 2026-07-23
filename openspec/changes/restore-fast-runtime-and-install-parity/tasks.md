## 1. Regression Gates

- [x] 1.1 Add failing structural tests proving warm semantic-unit recall does not walk/parse every parent and outside-KB widening does not rebuild vault BM25 state.
- [x] 1.2 Add failing structural tests proving repeated and post-unrelated-change `observe_memory` validation/commit do not parse the full corpus.
- [x] 1.3 Add the aggregate-only 2k/8k foreground benchmark and enforce median, p95, and scaling thresholds in CI.

## 2. Event-Maintained Semantic Corpus State

- [x] 2.1 Add an event-token hot path to the bounded process cache while retaining exact config and census fallbacks.
- [x] 2.2 Implement exact-path Markdown create/edit/delete reconciliation and stable-topology candidate replacement with full-build equivalence tests.
- [x] 2.3 Wire canonical writers and watcher batches to patch the warm context, and start cold semantic warm-up in the background.
- [x] 2.4 Absorb bounded Markdown churn during cold build instead of discarding the completed context.
- [x] 2.5 Expose semantic-corpus readiness and reject restart-window mutations as retryable before acquiring the mutation boundary.

## 3. Bounded Recall Paths

- [x] 3.1 Query/filter current semantic-unit sidecar rows first and hydrate only selected parents, preserving rich category/kind/tags/context and typed relations.
- [x] 3.2 Route outside-KB widening through maintained vault lexical rows while preserving the relaxed any-stem gate, reserved slots, and exclusions.
- [x] 3.3 Remove automatic unbounded filesystem fallbacks; retain the Python corpus only as an explicit operator rollback.

## 4. Fast Guarded Commit

- [x] 4.1 Seed the shared wikilink resolver from the exact corpus entries already parsed during preflight.
- [x] 4.2 Preserve existing canonical transaction, read-your-write, writer-fencing, and optional-worker behavior.
- [x] 4.3 Normalize explicit CRLF source snapshots so Windows deep-context memory references match disk-read indexing.

## 5. CLI And Service Install Parity

- [x] 5.1 Add cheap `--version`/JSON install provenance without optional dependency imports or private fields.
- [x] 5.2 Add the non-secret managed-install manifest and expose CLI/service version, profile, interpreter, and declared execution route.
- [x] 5.3 Update Windows and Unix upgrades to reconcile existing stale uv-tool commands to the verified live release without duplicating ML installs.
- [x] 5.4 Verify every PATH-visible `exomem`/`kb` executable and fail with an actionable exact-version repair command on drift.
- [x] 5.5 Preserve `exomem find` as a compatibility alias for current bounded `ask` behavior.

## 6. Verification And Delivery

- [x] 6.1 Run focused semantic, retrieval, writer, watcher, install, privacy, and OpenSpec tests plus the Windows-feasible lean matrix and ruff.
- [x] 6.2 Run the local 2k/8k gate and synthetic guarded commit/read-back; record only aggregate timing evidence.
- [x] 6.3 Update generic operator/release documentation for latency gates, install provenance, and CLI reconciliation recovery.
- [x] 6.4 Complete an independent code/security/performance review, address findings, and verify the final diff contains no private vault data.
- [ ] 6.5 After the merged release is deployed, run live connector validation/commit/read-back on the managed installation without recording private content.
