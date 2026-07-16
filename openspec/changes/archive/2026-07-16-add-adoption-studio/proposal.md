## Why

Exomem's confirmed product gap — validated by the KB product-flow benchmark — is **first-run adoption of existing messy material**. Today `adopt` is a stateless four-mode primitive (`scan-only` / `save-manifest` / `copy-as-sources` / `compile-selected`): selection is re-passed on every call, there is no durable run, no preview-then-confirm, no write-time re-validation, and no guided path from "here is my folder" to "first useful governed recall." A non-technical owner has no way to see what will happen before it happens, resume an interrupted import, or hand the imported material to a connected agent for structured help.

Adoption Studio is the fifth surface in the KB roadmap (Epistemic Inbox → governed review actions → stable `exomem://` contexts → Belief Evolution → **first-run adoption**). Per the settled overlay decision, adoption scans everything read-only and copies material in as governed Sources; **originals are never rewritten, moved, or deleted.** The capability must *feed the existing review-item and context object model* rather than inventing parallel review machinery.

Evidence: `src/exomem/adopt.py` is stateless with execute-then-report semantics (no run object, no confirm step); `src/exomem/review_state.py` + `src/exomem/relation_queue.py` already provide the propose → review → governed-write pattern this change reuses; `src/exomem/studio/` ships a packaged Studio app with zero adoption UI.

## What Changes

- Add one durable, resumable, canonical-file-backed **adoption run** object under `Knowledge Base/_Adoption/runs/<run_id>/` (`run.json` + `proposals.json`), written only through `vault.batch_atomic_write` so it inherits the access-tier backstop and writer-lease fence with no new persistence engine and no new write primitive.
- Add one new product command, **`adoption_studio`**, with ten actions (`start`, `status`, `select`, `plan`, `apply`, `cancel`, `finish`, `work-item`, `propose`, `apply-proposal`). Because it is a single `_PRODUCT_SPEC` registry entry, it is automatically exposed on MCP, REST (`POST /api/adoption_studio`), CLI, and the hosted gateway contract. Product surface goes 22 → 23 tools.
- Give adoption an explicit state machine (`selecting → planned → applying → applied|partial|failed → done`, plus `cancelled`) with a preview-exact-actions contract (`plan_id` + `selection_hash`), per-item write-time re-validation, partial-failure outcomes, retry of the failed subset, interrupted-apply recovery, and cancellation rules.
- Add a three-layer fingerprint model — stat-level inventory fingerprint (cheap staleness), per-source sha256 captured at `plan` and re-verified per item at `apply`, and per-proposal content-hash bindings re-verified at `apply-proposal` plus each governed op's own compare-and-swap.
- Add the **AdoptionWorkItem** agent contract (bounded deterministic context pack) and structured-proposal submission for five proposal kinds (`compilation`, `entity`, `relation`, `supersession`, `reconciliation`), validated two-phase and applied *only* through the existing governed leaves (`remember`, `link`, governed `edit`, `replace_memory`) — no arbitrary mutation path.
- Surface proposals as `exomem://review/adoption/<id>` items through the existing verbs (`review_memory(mode="adoption")`, `triage_memory`, `review_item_context`), a zero-argument `continue_adoption` MCP prompt, and an `exomem://adoption/run/<id>` MCP resource.
- Refactor `adopt._copy_as_sources` into behavior-preserving halves (`plan_import_items` / `commit_import_items`) reused by the run engine; add `context_refs.adoption_run_ref`. All existing `tests/test_adopt.py` cases stay green.
- Add the **Adoption Studio UI** as a same-app top-level Studio view (`?view=adopt`) — nine guided screens — with forced immutable-cache filename bumps (`app.v4.js`, `state.v2.js`, `styles.v2.css`, new `adoption.v1.js` + `adoption-model.v1.js`, `manifest.json` version 4).
- Add the thin **hosted entrypoint**: adoption uploads/ZIPs land as raw files under vault-relative `_Staging/adoption/<run_id>/` (outside `Knowledge Base/`, ZIP expanded cell-side with zip-slip protection and entry/size caps in `hosted_transfer_routes.py`); `adoption_studio(start)` accepts `path` at that staging dir. Hosted admission is generic — `invocation_is_read_only` classifies `status`/`work-item` as reads and every other action as a mutation — so `adoption_studio` is **not** added to the hosted intercept set.
- Add the **Substrate Home integration spec** (design.md) so a separate team can mount adoption in Home without design work.

## Capabilities

### New Capabilities

- `adoption-studio`: A governed, durable, resumable adoption session over existing material — deterministic read-only scan, folder-rule selection materialized server-side, exact-action preview, governed apply with write-time re-validation and per-item outcomes, cancellation/retry/stale detection, the AdoptionWorkItem agent contract with five structured proposal kinds routed only through existing governed leaves, review-item surfacing, first-question handoff, and the originals-never-modified invariant.

### Modified Capabilities

- `command-surface`: Adds `adoption_studio` as a single registry entry generating every surface, with a read-only action allowlist (`status`, `work-item`) resolved by `invocation_is_read_only`, and extends `review_memory` / `triage_memory` / `review_item_context` to dispatch adoption refs. The golden MCP schema fixture is intentionally regenerated for exactly these four tools.
- `hosted-gateway-contract`: `adoption_studio` is admitted through the generic registry path (read/write classified by `invocation_is_read_only`), never intercepted; adoption uploads/ZIPs land in a vault-relative per-run staging area under `_Staging/adoption/<run_id>/` with zip-slip protection and entry/size caps.

## Impact

- Adds `src/exomem/adoption_run.py` and `src/exomem/adoption_proposals.py`; extends `src/exomem/commands.py` (new leaf, registry entry, read-only allowlist, review/triage/context dispatch), `src/exomem/adopt.py` (refactor + manifest helpers), `src/exomem/context_refs.py`, and `src/exomem/server.py` (MCP prompt + resource).
- Adds the packaged Studio adoption view and forces asset filename bumps under `src/exomem/studio/` (immutable caching); updates `src/exomem/studio/manifest.json` to version 4.
- Adds the thin hosted entrypoint in `src/exomem/hosted_transfer_routes.py` (staging landing + ZIP expansion) only; touches no other hosted module and does not edit `src/exomem/writer_lease.py`.
- Intentionally regenerates `tests/fixtures/mcp_tool_schemas.json` for `adoption_studio`, `review_memory`, `triage_memory`, `review_item_context`; all other existing suites (including `tests/test_adopt.py`, `tests/test_scaffold_no_leak.py`) stay green.
- Out of scope: Drive/Dropbox/Notion/Apple Notes connectors, any server-side reasoning LLM, teams/shared vaults, production deploy infrastructure, and changes to the Substrate control-plane repository (its integration is specified but implemented separately).
