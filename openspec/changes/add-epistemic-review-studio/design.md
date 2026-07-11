## Context

Exomem 0.16 already exposes the differentiating substrate needed for a human review product: stable `exomem://review/<id>` items, fingerprint-bound triage state, deterministic attention ranking, canonical memory references, typed relations, graph context, provenance, read/history operations, and recorded supersession evolution. The follow-on corpus-activation change adds a separate opt-in structural worklist. These capabilities are available through product commands, but there is no browser surface that composes them into a legible daily loop.

The current service is a Python/FastMCP application with Starlette custom routes, a registry-generated authenticated REST facade, and only a few packaged public assets. The Studio must preserve one leaf implementation per operation, work in local and remote personal deployments, add negligible idle cost, and never introduce a server-side reasoning model. The first release is a single-user review control plane, not a general note application.

## Goals / Non-Goals

**Goals:**

- Make the Inbox and activation backlog understandable and actionable from one browser entry point.
- Compose one bounded, deterministic review-item context from existing governed primitives.
- Keep every mutation explicit, attributable, and routed through the existing product command surface.
- Visualize recorded belief evolution without synthesizing a narrative or confidence judgment.
- Package the UI inside the Python distribution with no CDN, resident frontend service, or mandatory heavy dependency.
- Preserve local-first operation while respecting the existing REST and Cloudflare Access security boundaries.

**Non-Goals:**

- No rich Markdown editor, free-form file manager, generic graph canvas, hosted multi-tenant service, cloud sync, teams, CRDT collaboration, billing, or public sharing.
- No automatic compilation, relationship acceptance, supersession, dismissal, or vault cleanup.
- No server-side reasoning model, agent daemon, background review job, or new source of epistemic scores.
- No reimplementation of Inbox ranking, activation scanning, graph traversal, provenance, evolution, or governed writes in the browser.

## Decisions

### Ship a standards-native static application inside the Python package

The Studio will live under a packaged `studio/` asset directory and be served at `/studio/` by the existing FastMCP/Starlette process. The first version will use semantic HTML, CSS, and small ES modules without a Node runtime, CDN, or frontend framework. A manifest-backed asset helper will serve only known packaged files with fixed content types, cache immutable assets, and keep the HTML shell uncached.

This matches the intentionally thin product: worklist, inspector, actions, and timelines. It keeps wheel builds reproducible, offline operation intact, and startup/idle cost negligible. A compiled frontend framework was rejected for the first slice because it adds a second toolchain and generated artifact lifecycle before the interaction model has earned that complexity. Server-rendered pages were rejected because stateful filtering, optimistic triage, and timeline exploration benefit from a small client application.

If the packaged assets are absent or invalid, Studio routes return a bounded diagnostic response and the MCP/REST/CLI service continues normally. The UI is additive and soft-failing, never a server-readiness dependency.

### Keep the browser shell separate from the authenticated data plane

The static shell contains no vault content and may be served without disclosing user data. Every data read or write goes through same-origin `/api/*` routes and the existing REST authorization gate: bearer API key for personal REST deployments or Cloudflare Access identity where configured. The Studio will keep a manually supplied bearer credential in memory or `sessionStorage`, never `localStorage`, cookies created by the Studio, URL query parameters, rendered HTML, or logs. Remote deployments behind Cloudflare can rely on the existing identity header without exposing a bearer key to JavaScript.

The browser client will refuse cross-origin API bases, send a strict same-origin policy, and the static response will set a restrictive Content Security Policy. No route bypasses REST authentication merely because a request originates from loopback. A convenient launcher may open `/studio/`, but it does not place secrets in the URL. An unauthenticated user can see the inert shell and setup guidance, never counts, titles, paths, or note content.

Alternatives rejected: a loopback authentication bypass is vulnerable to hostile local-origin requests; embedding the REST key in a URL leaks it through history and logs; adding a new cookie/session system duplicates the existing personal-service boundary.

### Add one bounded `review_item_context` product command

The Studio needs one coherent inspection response rather than a waterfall of browser-specific joins. A new read-only Tier 1 command, `review_item_context`, accepts a stable review reference plus explicit graph/body/version bounds. Its leaf resolves the current item across the daily Inbox and, after its prerequisite lands, the activation queue; reads the target; gathers bounded related-page summaries; composes provenance/evidence, graph neighborhood, recorded history, and path-specific supersession evolution; and reports truncation and unavailable sections independently.

The command is generated across MCP, REST, and CLI from the registry. It calls shared leaf helpers and returns source data, never rendered HTML. Deterministic sections remain useful if embeddings, optional graph enrichment, or a supersession chain are absent. It runs no model and makes no semantic judgment. Access-policy checks happen before content is included, and a disappeared or fingerprint-changed item returns an explicit stale/not-found result so the client refreshes the worklist.

Alternatives rejected: browser-side joining couples the UI to many command shapes and creates partial-loading races; a Studio-only endpoint violates surface consistency; returning whole related documents creates unbounded payload and unnecessary disclosure.

### Treat the UI as a command client, not another mutation layer

Dismiss, snooze, and reopen call `triage_memory`. Relation work starts from `connect_memory` suggestions and requires a separate explicit acceptance/edit action. Compilation uses `compile_source` for a read-only proposal before `remember`. Supersession shows a diff/preview and then calls `replace_memory` only after confirmation. The Studio never writes Markdown directly and never turns a model suggestion into a durable edge automatically.

The client may optimistically update a row only after a successful command response. Destructive or conclusion-changing operations require a review screen that names the target, action, and consequence. Errors retain the draft and leave the worklist state unchanged.

### Make recorded evolution a view inside the same review workspace

The Evolution panel consumes pointer-ordered supersession versions, recorded transition reasons, structural claims, dates, and provenance already held by Exomem. A path-specific helper avoids topic-search ambiguity when the user opens a known review target. The visualization may connect versions and evidence, but labels every transition with recorded data and displays honest empty/truncated states. It does not generate a summary of how beliefs changed or assign quality, confidence, or authority.

### Design the Studio around one narrow end-to-end proof loop

The primary route opens on the ranked Inbox. Selecting an item preserves its stable reference in the route fragment, loads its bounded context, and exposes only actions valid for that item. Activation is a separate explicitly selected worklist with denominator-backed coverage; it never pollutes the daily Inbox. Filters, state tabs, selection, and back/forward navigation remain client state, while the vault and review-state file remain the source of truth.

The acceptance path is: open Studio, understand why an item surfaced, inspect cited context, take or decline one governed action, and see the queue update. Search, generic browsing, and authoring can follow later if real use shows they improve this loop.

## Risks / Trade-offs

- [Risk] A standards-native client becomes hard to maintain as the surface grows. -> Keep modules organized by command and view, enforce a small state model, and revisit a compiled framework only after the MVP interaction contract stabilizes.
- [Risk] The existing REST API key flow is awkward in a browser. -> Provide clear session-scoped credential setup and rely on Cloudflare Access where configured; defer a new auth/session system rather than weakening the boundary.
- [Risk] One composed context command becomes an expensive mega-call. -> Set conservative body, node, edge, related-page, and version defaults; return per-section truncation/timing; reuse caches and shared helpers; add a latency regression test.
- [Risk] Activation is not yet on `main`. -> Treat its merge as an explicit prerequisite and keep the Inbox path independently functional if activation is unavailable.
- [Risk] UI actions could make governed writes feel casual. -> Separate proposals from commits, require explicit confirmation for conclusion-changing actions, show the exact target, and preserve server-side validation and audit logging.
- [Risk] Packaged assets enlarge or destabilize the wheel. -> Keep assets small and source-controlled, test wheel inclusion, and make asset failure soft so core service readiness is unaffected.
- [Trade-off] The first release will not match Basic Memory's editor/team breadth. -> It deliberately proves Exomem's distinct review-and-evolution loop before investing in parity surfaces.

## Migration Plan

1. Merge the existing-corpus activation prerequisite and sync its OpenSpec contract.
2. Add and test the read-only `review_item_context` leaf and registry entry with no UI dependency.
3. Add packaged Studio assets, static routes, CSP, and an authenticated API client with empty/error states.
4. Implement Inbox and activation worklists, then the item inspector and triage actions.
5. Add proposal/confirmation flows for connect, compile, and supersede, followed by the recorded Evolution panel.
6. Add wheel-install, browser workflow, accessibility, auth-boundary, and latency acceptance tests; document `exomem studio` and remote deployment behavior.

Rollback removes the Studio routes/assets and the additive read command. Existing review state, notes, relations, history, and product commands remain valid and unchanged.

## Open Questions

- Whether the convenience launcher should remain a URL printer or also open the system browser; implementation should default to printing and make browser launch explicit for headless/service environments.
- Whether a later release should introduce a dedicated local session exchange. The MVP must use the existing REST/Cloudflare boundary and must not block on a new authentication subsystem.
