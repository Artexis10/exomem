## 1. Prerequisite And Contract Baseline

- [ ] 1.1 Rebase the implementation branch after the existing-corpus activation change lands and verify `review_memory(mode="activation")` plus stable activation item lookup are available without copying that change's scanner or ranking logic.
- [ ] 1.2 Add contract fixtures for `review_item_context` inputs, bounds, result sections, truncation, unavailable-section metadata, and stale-fingerprint errors before implementing the command.
- [ ] 1.3 Add a path-specific evolution contract test covering pointer order, similar-title isolation, recorded transition reasons, single-version empty state, and version truncation.

## 2. Bounded Review Item Context

- [ ] 2.1 Refactor the existing evolution helpers to expose a read-only path/reference-specific chain builder while preserving the current topic-query evolution response.
- [ ] 2.2 Implement a `review_context` assembly module that resolves attention or activation items by stable review reference and optional expected fingerprint.
- [ ] 2.3 Compose bounded target body, related-page summaries, canonical references, provenance/evidence, graph neighborhood, history, current review state, and path-specific evolution with independent availability and truncation metadata.
- [ ] 2.4 Enforce existing read/access policy and content minimization for target and related material, including explicit target-denied and item-changed errors.
- [ ] 2.5 Register `review_item_context` as one read-only Tier 1 product command and regenerate MCP schemas, REST/OpenAPI coverage, CLI help, and `docs/capabilities.md` from the registry.
- [ ] 2.6 Add unit and product-surface tests proving deterministic output, bounds, no vault mutation, no model load, partial-section soft failure, and equivalent MCP/REST/CLI result shapes.
- [ ] 2.7 Add a latency regression test for the default context bounds and verify the command reuses existing parsed/indexed data rather than reparsing the full vault per section.

## 3. Packaged Studio Shell And Security Boundary

- [ ] 3.1 Create the source-controlled Studio asset layout with semantic HTML, CSS, ES modules, icons, and a manifest; add package-data configuration and installed-wheel inclusion tests.
- [ ] 3.2 Add `/studio/` and manifest-allowlisted asset routes with correct content types, immutable asset caching, uncached shell HTML, path-traversal rejection, and soft-fail diagnostics when assets are missing.
- [ ] 3.3 Add restrictive Content Security Policy and related browser security headers while keeping all runtime requests same-origin and free of CDN/external asset dependencies.
- [ ] 3.4 Implement the small REST client and session-scoped authentication screen for bearer-key and Cloudflare Access deployments without putting secrets in URLs, HTML, logs, persistent cookies, or `localStorage`.
- [ ] 3.5 Add server-route and auth-boundary tests proving unauthenticated clients receive no vault-derived data and Studio failures do not affect MCP, REST, CLI, health, or retrieval readiness.
- [ ] 3.6 Add an `exomem studio` convenience command that prints the resolved Studio URL by default and opens a browser only behind an explicit option suitable for interactive hosts.

## 4. Inbox And Activation Worklists

- [ ] 4.1 Implement a minimal client state/router keyed by worklist mode, filters, state tab, and stable review reference, including browser back/forward restoration and stale-selection recovery.
- [ ] 4.2 Render the daily Inbox in server order with category/reason labels, state and summary counts, truncation notes, loading/empty/error states, and no client-side epistemic reranking.
- [ ] 4.3 Render corpus activation as a separately selected opt-in worklist with structural categories and denominator-backed coverage, and handle activation-unavailable deployments without breaking the Inbox.
- [ ] 4.4 Add keyboard list navigation, visible focus, labelled filters and status, responsive narrow/desktop layouts, and non-color-only severity/state indicators.
- [ ] 4.5 Add deterministic UI fixture tests for attention ordering, filters, counts, truncation, stable selection, activation separation, and honest empty/error states.

## 5. Review Workspace And Triage

- [ ] 5.1 Render the selected target, exact review reasons, related summaries, canonical references, provenance/evidence, graph neighborhood, history, and independent unavailable/truncated section states from `review_item_context`.
- [ ] 5.2 Implement dismiss, snooze, and reopen dialogs through `triage_memory`, updating the worklist only after server success and preserving state plus actionable errors on failure.
- [ ] 5.3 Add stale-fingerprint handling that refreshes the worklist and makes the changed signal visible instead of applying an action to obsolete context.
- [ ] 5.4 Add keyboard-complete workspace navigation, focus management for dialogs and errors, and restoration of list position after a successful or cancelled action.

## 6. Governed Proposal And Write Flows

- [ ] 6.1 Add relation suggestion display through `connect_memory`, clearly label model-backed output as provisional, and require a separate validated `edit_memory` confirmation to persist an accepted governed relation.
- [ ] 6.2 Add source-compilation planning through `compile_source`, retain the proposed note as an editable draft, and require explicit `remember` confirmation before creating compiled knowledge.
- [ ] 6.3 Add supersession preview showing target, successor draft, reason, and consequence, then call `replace_memory` only after explicit confirmation; cancellation and errors must perform no write.
- [ ] 6.4 Add integration tests proving proposals never mutate, confirmed writes use existing commands and audit logging, validation errors preserve drafts, and the reviewed signal refreshes or leaves the queue according to its new fingerprint.

## 7. Recorded Evolution View

- [ ] 7.1 Render a pointer-ordered version timeline with recorded dates, structural claims, transition reasons, provenance, and canonical references from the context response.
- [ ] 7.2 Add honest single-version, unavailable, and truncated states and verify the client never generates transition narrative, confidence, authority, or causal labels.
- [ ] 7.3 Add accessible timeline semantics and keyboard navigation, including a non-visual tabular/list representation of every displayed relationship.

## 8. Verification, Packaging, And Documentation

- [ ] 8.1 Add an opt-in Playwright browser acceptance lane covering authentication, Inbox inspection, triage, activation separation, governed proposal confirmation, evolution, back/forward navigation, narrow viewport, and keyboard-only operation without making browser tooling a runtime dependency.
- [ ] 8.2 Run focused Studio/context tests, `uv run ruff check`, the lean full suite with embeddings disabled, generated-capability checks, REST/MCP schema fixtures, scaffold leak guard, startup benchmark, and latency gate.
- [ ] 8.3 Build wheel and sdist artifacts, install the wheel into a clean environment, verify offline Studio assets/routes and the full read/triage lifecycle, and confirm package-size growth remains within the documented budget.
- [ ] 8.4 Document the Review Studio quickstart, auth behavior, local and remote deployment, measurement-only semantics, explicit write confirmations, activation prerequisite, and deliberate non-goals versus a generic notes app.
- [ ] 8.5 Record the shipped end-to-end product proof in the product gap matrix and Exomem Knowledge Base after acceptance, including known limitations and the next measured product decision.
