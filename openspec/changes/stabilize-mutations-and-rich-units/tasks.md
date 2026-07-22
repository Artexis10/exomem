## 1. Rich semantic projection

- [x] 1.1 Add failing parser and authoring tests proving exact rich tag/context normalization, deduplication, invalid-field diagnostics/fallback, and separation from category, kind, parsed metadata, and relations.
- [x] 1.2 Implement rich tag/context normalization at the unified parse boundary and make the focused parser/authoring tests pass.
- [x] 1.3 Add and pass retrieval/index regressions for rich `unit.tags` and `unit.context` filters, full hits, exact reads, lexical records, graph nodes, and context packs; bump parser/generation and affected sidecar identities and prove an explicit quiesced post-upgrade reconcile rebuilds unchanged Markdown into lexical, enabled vector, and graph projections without rewriting bytes or mtimes; clarify heading-derived category identities in public docs.

## 2. Mutation classification and configuration

- [x] 2.1 Add failing tests proving `replace_memory(validate_only=true)` returns an advisory hash-bound preview while bypassing writer authority, mutation locking, and receipts, and that a real replacement freshly revalidates before mutation.
- [x] 2.2 Add failing tests proving the default manager uses `LeaseConfig.mutation_timeout_seconds`, then implement the preview classification and timeout wiring.

## 3. Process-safe holder observability

- [x] 3.1 Add deterministic cross-process tests proving status reports the current OS-lock generation, a busy waiter carries bounded verified/unknown holder diagnostics, stale or malformed metadata cannot report a false verified holder, and paused acquire-to-publish plus probe-to-cleanup interleavings are safe.
- [x] 3.2 Implement the metadata-mutex handshake, atomic content-free holder metadata, authoritative nonblocking lock probes, exact-vault coordination status, and safe release/crash behavior.
- [x] 3.3 Cover readiness/status privacy and multi-vault isolation, including unknown external-holder fallback and no vault path, content, credential, or tenant leakage.

## 4. MCP application-error contract

- [x] 4.1 Add failing wrapper and FastMCP integration tests proving public `OpError` outcomes are normal `success:false` tool content with full mutation retry details while unexpected exceptions remain native tool errors.
- [x] 4.2 Implement the MCP application-error mapping and prove repeated busy outcomes do not block a later read or duplicate a commit within the same effective retry identity; document that receipts are not transferable cross-session keys.
- [x] 4.3 Update the generic skill/runtime documentation to teach structured retry outcomes and cross-process status without changing the MCP input-schema baseline or leaking private vault context.

## 5. Bounded media mutation authority

- [x] 5.1 Add concurrency regressions proving watcher, startup, and explicit `process_media` provenance hashing yield the global boundary to foreground mutations, then commit under named writer-fenced per-artifact guards.
- [x] 5.2 Route pathless process/retry through per-artifact commit callbacks, revalidate binary/access/confinement/CAS state at commit, and keep mutation classification plus retry identity intact.
- [x] 5.3 Move background sidecar derived fanout and pure re-embed work outside the global boundary; add content-free operation/holder metadata to retained media-worker guards and document the remaining extraction/failure/CLIP/scene-frame fanout scope.

## 6. Verification and delivery

- [x] 6.1 Run focused red-green suites for semantic projection, command classification, writer lease, mutation locking, coordination status, runtime readiness, media concurrency, and FastMCP transport.
- [x] 6.2 Run the OpenSpec validator, scaffold leak gate, schema-fidelity gate, lint/type checks, and the proportionate non-embedding regression suite.
- [ ] 6.3 Obtain independent concurrency/contract review, fix all blocking or important findings, then commit, integrate current `origin/main`, push, and open a ready PR with verification evidence.
