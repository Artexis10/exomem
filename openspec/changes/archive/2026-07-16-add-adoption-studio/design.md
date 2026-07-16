## Context

Exomem is a deterministic, agent-native, governed knowledge substrate (Markdown vault + derived indexes, MCP-first). `adopt.py` already ships four stateless modes (`scan-only`, `save-manifest`, `copy-as-sources`, `compile-selected`) with execute-then-report semantics: selection is re-passed per call, there is no run object, no preview-then-confirm, and no write-time re-validation. The review identity stack — `review_state.py` (item id + fingerprint + `exomem://review/<id>` + portable JSON state), `relation_queue.py` (the propose → review → governed-write reference), `replace.py` (supersession CAS + single atomic batch) — is the machinery adoption must *feed*, not duplicate. The command registry `_PRODUCT_SPEC` in `commands.py` is the host-neutral seam: a registered product command is auto-published to MCP, REST, CLI, and the hosted gateway contract. Hosted runtime is merged on main (PRs #227, #233); the generic hosted command route classifies read/write by `invocation_is_read_only` and only intercepts `transfer_artifact` / `adopt_vault` by leaf identity.

This design is the reconciled union of the engine brief (backend contracts), the UX brief (copy and screen contracts), and the approved plan's pinned UI↔engine vocabulary. Where the briefs conflicted, the plan wins on shared vocabulary, the engine brief wins on backend contracts, and the UX brief wins on copy.

## Goals / Non-Goals

- Goal: a durable, resumable, canonical-file-backed adoption run with a preview-exact-actions contract and write-time re-validation.
- Goal: one new product command; everything agent/review-facing rides existing verbs.
- Goal: originals are never rewritten, moved, or deleted, under any action or failure.
- Goal: a host-neutral engine that hosted consumes with a thin staging entrypoint and zero bespoke command routing.
- Non-Goal: connectors (Drive/Dropbox/Notion/Apple Notes), any server-side reasoning LLM, teams/shared vaults, deploy infrastructure, or Substrate control-plane implementation (specified here, built separately).
- Non-Goal: a new persistence engine, a new write primitive, a parallel review queue, or edits to `writer_lease.py`.

## Decisions

### 1. One product command `adoption_studio` with ten actions

New leaf `op_adoption_studio(vault_root, action, ...)` placed after `op_adopt_vault`, dispatching `start | status | select | plan | apply | cancel | finish | work-item | propose | apply-proposal` to `adoption_run.*` / `adoption_proposals.*`. Full signature (drives the MCP inputSchema; every param carries a Google-style Args line):

```python
def op_adoption_studio(
    vault_root: Path,
    action: str,
    run_id: str | None = None,
    path: str = "",
    include_hidden: bool = False,
    initialize_kb: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    overrides: list[str] | None = None,
    include_junk: bool = False,
    plan_id: str | None = None,
    retry_failed: bool = False,
    only_paths: list[str] | None = None,
    why: str | None = None,
    write_manifest: bool = True,
    sources: list[str] | None = None,
    max_sources: int = 5,
    max_chars_per_source: int = 2000,
    proposals: list[dict] | None = None,
    ref: str | None = None,
    expected_fingerprint: str | None = None,
    expected_hash: str | None = None,
) -> dict: ...
```

Unknown action → `ValueError("INVALID_MODE: adoption_studio action must be start, status, select, plan, apply, cancel, finish, work-item, propose, or apply-proposal")`. `AdoptionRunError` / `AdoptionProposalError` re-raised as `ValueError(f"{code}: {reason}")` (house convention). Ten actions on one command is within precedent (`connect_memory` 7, `manage_memory_file` 7) and keeps the product surface at 23 tools.

**Selection vocabulary (plan override):** the pinned UI↔engine contract is the folder-rule payload `{include, exclude, overrides, include_junk}`, materialized server-side. `select` accepts these params; the engine materializes the concrete path set from the depth-capped tree, then validates each path against inventory (`NOT_IN_INVENTORY` / `UNSUPPORTED_IMPORT_TYPE` / `ALREADY_GOVERNED`, rejected per-path). The engine brief's internal `set_paths`/`add_paths`/`remove_paths` remain the private path-level mechanism `adoption_run.select` uses after materialization; the public product params are the rule shape.

Registry entry (append to `_PRODUCT_SPEC`):

```python
(
    "adoption_studio", op_adoption_studio,
    1,            # tier
    True,         # cli_writes → routes through writer_lease.invoke_command
    False,        # needs_schema
    None,         # positional
    _MCRC,
    ("adopt",),   # route references the canonical `adopt` leaf → validate_product_registry() passes
    {"surface": "primary", "actions": ("adopt", "review", "save"), "first_run_safe": True},
),
```

Read-only classification (near line 4223):

```python
_ADOPTION_STUDIO_READ_ONLY_ACTIONS = frozenset({"status", "work-item"})
```

plus a third branch in `invocation_is_read_only` resolving the `action` selector via `_resolved_invocation_selector`, exactly like the `adopt_vault` branch. `status`/`work-item` stay lease-free; all other actions get lease + implicit MCP retry replay. This one classification is also the hosted read/write admission decision (Decision 8).

Existing-verb extensions: `op_review_memory` adds `mode == "adoption"` → `adoption_proposals.build_queue`; `op_triage_memory` adds `is_adoption_ref(ref)` dispatch before the relation branch; `op_review_item_context` dispatches adoption refs to `adoption_proposals.assemble_context`. Each docstring change is an intentional golden-fixture regeneration (Decision 9).

### 2. Run store — `src/exomem/adoption_run.py` (new)

Storage: `Knowledge Base/_Adoption/runs/<run_id>/run.json` + `proposals.json`, written only through `vault.batch_atomic_write([PlannedWrite(...)], vault_root=root)` (inherits access-tier backstop + writer-lease fence). Module-level `threading.Lock` around load-modify-write, mirroring `review_state._LOCK`. `run_id` = `adr-<YYYYMMDD>-<sha256(source_root + created_iso + inventory_fingerprint)[:8]>`.

`run.json` schema (dataclass `AdoptionRun`, serialized as dict):

```json
{
  "schema_version": 1,
  "run_id": "adr-20260714-ab12cd34",
  "run_ref": "exomem://adoption/run/adr-20260714-ab12cd34",
  "created": "2026-07-14T09:00:00Z",
  "updated": "2026-07-14T09:05:00Z",
  "phase": "selecting",
  "source_root": "",
  "scan_summary": {"totals": {"files": 0, "dirs": 0, "markdown": 0, "binary": 0},
                   "kb": {"present": true, "path": "Knowledge Base"},
                   "junk_counts": {}, "skipped": {}},
  "pack_suggestions": [{"id": "...", "name": "...", "score": 0, "matched_signals": ["..."]}],
  "inventory": [
    {"path": "Old Notes/a.md", "bytes": 812, "mtime": 1752470000.0, "eligible": true, "reason": null},
    {"path": "Old Notes/img.png", "bytes": 51234, "mtime": 1752470001.0, "eligible": false,
     "reason": "UNSUPPORTED_IMPORT_TYPE"}
  ],
  "inventory_truncated": 0,
  "inventory_fingerprint": "sha256[:24] over sorted (path|bytes|mtime) of eligible rows",
  "selection": {"paths": ["Old Notes/a.md"], "selection_hash": "sha256[:16] of sorted paths",
                "rules": {"include": [], "exclude": [], "overrides": [], "include_junk": false},
                "updated": "2026-07-14T09:02:00Z"},
  "plan": {
    "plan_id": "sha256[:16] of (selection_hash + concatenated per-item sha256s)",
    "created": "2026-07-14T09:03:00Z",
    "selection_hash": "…must equal selection.selection_hash at apply…",
    "items": [
      {"original_path": "Old Notes/a.md", "original_sha256": "…64 hex…", "original_bytes": 812,
       "action": "copy-as-source",
       "target_path": "Knowledge Base/Sources/Imported/2026-07-14-title.md",
       "target_ref": "exomem://source/…", "title": "Title",
       "frontmatter": {"type": "source", "source_type": "other", "imported_from": "Old Notes/a.md",
                       "original_sha256": "…", "original_bytes": 812, "tags": ["imported"]}}
    ],
    "skipped": [{"path": "Old Notes/img.png", "code": "UNSUPPORTED_IMPORT_TYPE", "reason": "..."}],
    "warnings": []
  },
  "outcomes": {
    "Old Notes/a.md": {"status": "applied", "target_path": "...", "source_ref": "...",
                        "sha256": "...", "at": "..."},
    "Old Notes/b.md": {"status": "failed", "code": "SOURCE_CHANGED",
                        "reason": "sha256 mismatch at apply time", "at": "..."}
  },
  "finish": null,
  "cancel": null,
  "errors": []
}
```

Field notes: `outcomes[path].status ∈ applied | failed | skipped | already-applied`; failure `code ∈ SOURCE_CHANGED | NOT_FOUND | BATCH_ROLLED_BACK | UNSUPPORTED_IMPORT_TYPE`. `finish` (when set): `{at, recall_check:{query, ok, hits}, first_question, route:{tool:"ask_memory", args:{query}}, handoff:{prompt_text, links}, verified_unchanged, verified_total, manifest_path|null}`. `cancel` (when set): `{at, why}`. Inventory records cheap stat data only; per-source sha256 is computed lazily at `plan` for selected files and re-verified per item at `apply`. Plan items persist metadata, not rendered content (content re-rendered at apply from re-validated bytes, keeping `run.json` small). `max_candidates` default 5000 with explicit `inventory_truncated` (never a silent cap).

`proposals.json` schema:

```json
{
  "schema_version": 1,
  "run_id": "adr-20260714-ab12cd34",
  "proposals": [
    {"proposal_id": "sha256(canonical_json(kind + payload))[:24]",
     "review_id": "review_state.item_id('adoption:<run_id>:<proposal_id>')",
     "ref": "exomem://review/adoption/<review_id>",
     "fingerprint": "review_state.fingerprint(...) — see Decision 5",
     "kind": "compilation | entity | relation | reconciliation | supersession",
     "why": "agent's one-line provisional rationale (required)",
     "payload": {"...kind-specific..."},
     "bindings": {"run_fingerprint": "<must equal run's inventory_fingerprint at submit>",
                  "sources": {"Knowledge Base/Sources/Imported/2026-07-14-title.md": "<sha256>"}},
     "status": "proposed | invalid | applied",
     "findings": [{"code": "...", "path": "...", "detail": "..."}],
     "submitted_at": "2026-07-14T09:10:00Z",
     "applied": null}
  ]
}
```

`applied` (when set): `{at, result_path, result_ref, why}`. Dismiss/snooze/reopen decisions live ONLY in `.review-state.json` keyed `review_id:fingerprint` — `proposals.json.status` changes only for `applied`/`invalid` (single decision record; resurfacing for free).

`adoption_run.py` inventory: `AdoptionRunError(code, reason)`, `_LOCK`, `AdoptionRunStore` (`run_dir`, `load`→`RUN_NOT_FOUND`, `save`, `load_proposals`, `save_proposals`, `list_runs`), id/fingerprint helpers (`new_run_id`, `inventory_fingerprint`, `selection_hash`, `plan_id_for`), `build_inventory`, `probe_staleness`, the seven lifecycle actions (`start`, `status`, `select`, `plan`, `apply`, `cancel`, `finish`), and internals (`_validate_selection_paths`, `_recompute_phase`, `_next_actions`, `_write_run_manifest`, `_recall_check`).

### 3. State machine, transitions, and error vocabulary

Phases: `selecting`, `planned`, `applying` (transient, persisted before the first item write), `applied`, `partial`, `failed`, `done`, `cancelled`.

```
        start → [selecting] --select--> [selecting]     (select always invalidates plan)
                    | plan
                    v
                [planned] --select--> [selecting]
                    | apply
                    v
                [applying]  (persisted BEFORE first item write, so a crash is visible)
                 /    |    \
                v     v     v
          [applied] [partial] [failed]  --apply(retry_failed=true)--> [applying] → …
                \     |     /
                 v    v
                 [done]     (finish; allowed from applied|partial)

    [cancelled] ← cancel, allowed from selecting|planned|partial|failed
```

| Action | Allowed from | Guards / effects | Error codes |
|---|---|---|---|
| `start` | (no run) | `overview` scan → `scan_summary` + inventory + `pack_suggestions`; requires `Knowledge Base/`; `initialize_kb=true` → `init.init_vault` first (`FileExistsError` → proceed) | `KB_NOT_INITIALIZED` |
| `select` | `selecting`, `planned` | materialize `{include, exclude, overrides, include_junk}` server-side; validate each path; per-path rejects; any change clears plan → `selecting` | per-path `NOT_IN_INVENTORY`, `UNSUPPORTED_IMPORT_TYPE`, `ALREADY_GOVERNED`; `RUN_NOT_FOUND`, `INVALID_PHASE`, `INVALID_SELECTION` |
| `plan` | `selecting` (non-empty) | read + sha256 each selected file now; compute exact targets (unique-path reservation); persist plan → `planned` | `MISSING_SELECTION`, `RUN_NOT_FOUND`, `INVALID_PHASE` |
| `apply` | `planned`; or `partial`/`failed` with `retry_failed=true`; re-runnable from interrupted `applying` | (1) `plan_id` param == stored `plan.plan_id`; (2) `selection_hash` match else `PLAN_STALE`; (3) already-`applied` → `already-applied`; (4) write-time sha256 re-verify (`SOURCE_CHANGED` / `NOT_FOUND`; out-of-band target → fresh unique target); (5) persist `applying`, ONE `batch_atomic_write` for validated subset + indexes + log (rollback → `BATCH_ROLLED_BACK`). Phase recompute: all→`applied`, mix→`partial`, none→`failed` | `PLAN_STALE`, `PLAN_NOT_FOUND`, `ADOPTION_SOURCE_CHANGED`, `RUN_NOT_FOUND`, `INVALID_PHASE`; per-item codes |
| `cancel` | `selecting`, `planned`, `partial`, `failed` | record `{at, why}` → `cancelled`; applied Sources survive (append-only) | `CANCEL_DURING_APPLY`, `ALREADY_APPLIED`, `RUN_NOT_FOUND` |
| `finish` | `applied`, `partial` | recall check + first question + `handoff` block + optional manifest → `done`; proposals never block | `INVALID_PHASE`, `RUN_NOT_FOUND` |
| `status` | any | read-only; list or full doc + `staleness`/`stale_paths` + `interrupted` + `proposals_summary` + `next_actions` + `handoff` | `RUN_NOT_FOUND` |
| `work-item` / `propose` / `apply-proposal` | see Decision 5 | see Decision 5 | see Decision 5 |

**Pinned error vocabulary (plan):** `ADOPTION_SOURCE_CHANGED` (stale plan/apply → UI 409 banner, selection preserved on re-scan), `PLAN_STALE` (plan_id/selection-hash mismatch), per-item `SOURCE_CHANGED` / `UNSUPPORTED_IMPORT_TYPE` / `ALREADY_GOVERNED` / `NOT_FOUND`, `KB_NOT_INITIALIZED`, `REVIEW_ITEM_CHANGED` (proposal binding drift). `apply`/`finish` results carry `verified_unchanged` / `verified_total` (post-apply re-hash of originals); the UI's honesty line renders only from these real counts.

### 4. Three-layer fingerprint model

1. **Stat-level `inventory_fingerprint`** — sha256[:24] over sorted `(path|bytes|mtime)` of eligible rows; cheap `status` staleness probe (`stale_paths`) and the `run_fingerprint` proposals bind to.
2. **Per-source sha256** — captured at `plan`, re-verified per item at `apply` (`SOURCE_CHANGED`).
3. **Per-proposal `bindings`** — content hashes re-verified at `apply-proposal` (`REVIEW_ITEM_CHANGED`) plus each governed op's own CAS (`expected_hash`, replace `STALE_SUPERSEDE`).

Identity-only refs (like `proposal_ref`) are never used as staleness guards.

### 5. Agent contract — `src/exomem/adoption_proposals.py` (new)

Ref namespace: `ADOPTION_REVIEW_PREFIX = "exomem://review/adoption/"` with `is_adoption_ref` / `adoption_review_ref` / `parse_adoption_review_ref` (24-hex validation), byte-for-byte the relation-queue pattern, preserving the #198 isolation rule (adoption refs never resolve to/from attention/activation/relation). Identity reuses `review_state`: `proposal_id = sha256(canonical_json(kind + payload))[:24]` (dedup); `review_id = review_state.item_id(f"adoption:{run_id}:{proposal_id}")`; `fingerprint = review_state.fingerprint(target_ref=<primary target>, categories=[kind], reasons=[{"category": kind, "meta": {"signal_version": sha256(canonical_json(payload) + "|" + canonical_json(bindings))[:16]}}], related_refs=[<other bound refs>])` — payload or bound-hash change resurfaces a dismissed item.

**AdoptionWorkItem** (`work-item`, read-only) — bounded deterministic JSON, measurements plus recorded content only:

```json
{
  "run_ref": "exomem://adoption/run/adr-20260714-ab12cd34",
  "run_id": "adr-20260714-ab12cd34", "phase": "applied",
  "run_fingerprint": "<inventory_fingerprint>",
  "constraints": "Your interpretation is explicit and provisional. You cannot write to the vault. Submit structured proposals via adoption_studio(action='propose'); each is validated, fingerprint-bound, reviewed by the user, and applied only through governed operations. Original files are never rewritten, moved, or deleted.",
  "sources": [{"original_path": "Old Notes/a.md", "sha256": "…", "bytes": 812, "title": "Title",
               "imported_path": "Knowledge Base/Sources/Imported/2026-07-14-title.md",
               "source_ref": "exomem://source/…", "excerpt": "<= max_chars>", "excerpt_truncated": true}],
  "measurements": {"pack_suggestions": [], "totals": {}, "junk_counts": {}},
  "existing_context": [{"for": "Knowledge Base/Sources/Imported/2026-07-14-title.md",
                        "related": [{"path": "…", "ref": "exomem://…", "title": "…", "type": "insight"}]}],
  "proposal_kinds": {"compilation": "...", "entity": "...", "relation": "...",
                     "reconciliation": "...", "supersession": "..."},
  "limits": {"max_sources": 5, "max_chars_per_source": 2000, "shown": 5, "total": 12, "truncated": 7}
}
```

`sources` selection: explicit `sources=[paths]` param else the first `max_sources` applied items; truncation always explicit. `existing_context` uses `corpus_aware.suggest_related` (the same retrieval `compile_proposal.propose_compilation` uses), max 5 per source. Excerpts: post-apply from the imported copy's `## Capture` fence, pre-apply from original bytes.

**Proposal submission** (`propose`, writes only `proposals.json`) — `{run_id, proposals: [<ProposalIn>]}`, each with `kind`, required `why`, `payload`, `bindings`. Kind payloads, validation targets, and governed apply routing:

| kind | payload | validated against | applied through (ONLY) |
|---|---|---|---|
| `compilation` | `sources` (governed `Knowledge Base/Sources/**.md`), `title`, `note_type`, `content`, optional `tags`, `project` | `note_type` ∈ schema note types (pre-check as `note()` does); sources resolve and are governed Sources; content 1–100 000 chars, non-base64 | `commands.op_remember(content, title, note_type, sources, …)` |
| `entity` | `entity_type`, `name`, `summary`, optional `slug`, `why_in_kb`, `tags`, `connections` | `entity_type ∈ link.ENTITY_TYPES` (person/concept/library/decision); name/summary non-empty | `commands.op_link(…)` (same leaf as `connect_memory(operation="create-entity")`) |
| `relation` | `from`, `to`, `relation_type` | `relation_registry.load_registry` resolves `relation_type` (unknown → `UNKNOWN_RELATION_TYPE`; agents cannot extend the registry); both endpoints exist | governed section append — `_accept_relations_edit` closure pattern: `edit(heading="Relations", section_position="append", new_string=bullet, expected_hash=<captured at apply>, create_missing_section=True)` |
| `supersession` | `old_path`, `title`, `note_type`, `content`, optional `tags`/`sources` | old page exists, not Sources/Evidence, not already superseded (mirrors `replace.py`); binding records old-page `content_hash` | `commands.op_replace_memory(old_path, reason=why, content, …)` → `replace.replace` (internal CAS = final race guard, `STALE_SUPERSEDE`) |
| `reconciliation` | `subject_path`, `duplicate_of`, `resolution: "relate"|"supersede"`, plus the resolved sub-kind's fields (`relation_type` default `duplicates`, must exist; or supersession fields) | union of the sub-kind's validation; both pages exist | routes to the relation edit or `replace_memory` path per `resolution` |

Two-phase validation: at `propose`, full structural + registry/schema validation (invalid → `status:"invalid"` + findings, never appliable; valid → `proposed` with ref + fingerprint); at `apply-proposal`, everything re-validated live — `expected_fingerprint` REQUIRED and must match; every `bindings.sources` path re-hashed (mismatch → `REVIEW_ITEM_CHANGED`); target pages re-checked; the governed op's own guards fire last; `why` required (relation-queue's required-not-optional stance). **No arbitrary mutation path:** `propose` writes exactly one JSON file; every applied effect routes through a pre-existing governed leaf.

Review surfacing: `review_memory(mode="adoption")` → `build_queue` (per-run grouped, effective state via `ReviewStateStore.effective_state`); `triage_memory` → `triage` (recompute fingerprint, `REVIEW_ITEM_CHANGED` on mismatch, then `ReviewStateStore.apply`); `review_item_context` → `assemble_context` (bounded pack + live binding check); approval on `adoption_studio(action="apply-proposal")` (mirrors relation acceptance living on `connect_memory`, not `triage_memory`).

`adoption_proposals.py` inventory: `AdoptionProposalError`, the ref helpers, `work_item`, `propose`, `build_queue`, `triage`, `assemble_context`, `apply_proposal`, and internals `_validate`, `_proposal_fingerprint`, `_resolve`, `_rehash_bindings`, `_route_apply`.

### 6. `adopt.py` refactor and `context_refs.py`

Split `_copy_as_sources` into behavior-preserving halves consumed by both the legacy stateless mode and `adoption_run`:

- `plan_import_items(root, selected_paths, *, today) -> tuple[list[ImportItem], list[dict]]` — resolution (`_resolve_selected_text_file`), read + sha256, title/slug, unique-target reservation (`_unique_import_path`), rendered content (`_render_imported_source`). Pure; no writes. `ImportItem = {original_path, rel, sha256, bytes, title, target, content}`.
- `commit_import_items(root, items, *, today) -> dict` — the existing Sources-index / top-index / log block + single `batch_atomic_write`, returning `{copied_sources, warnings}`.
- `_copy_as_sources` becomes the two glued together; external result shape unchanged; all `tests/test_adopt.py` stay green.
- Expose the manifest helpers for `adoption_run.finish`'s optional run manifest (`Knowledge Base/_Adoption/<date>-adoption-run-<run_id>.md`, embedded JSON summary).

`adoption_run.plan` calls `plan_import_items` (persisting item metadata, not content); `adoption_run.apply` re-validates then calls `commit_import_items` for the validated subset. `context_refs.py` adds `adoption_run_ref(run_id) -> f"{SCHEME}://adoption/run/{_encode(run_id)}"`.

### 7. First-question handoff and MCP prompt/resource

`finish` handoff is deterministic, modeled on `demo.py`'s recall proof: candidate query = title of the largest applied import (fallback: top `pack_suggestions[0].name` tokens) → `find.find(root, query, mode="keyword", limit=5, graph=False)` (keyword mode is deterministic in lean/test environments) → `recall_check = {query, ok: <imported target in hits>, hits}` (on failure, include a `maintain_memory(mode="reconcile")` hint rather than failing finish) → `first_question = f'What do my notes say about "{title}"?'` with route `{tool: "ask_memory", args: {query: title}}`.

Agent handoff backbone (works in every client): MCP tools + a copyable `prompt_text` embedding the stable run ref. Progressive enhancements in `server.py`: a zero-argument MCP prompt `continue_adoption` (server infers the newest open run; surfaces in claude.ai/Desktop "+" menu and `/mcp__exomem__…` in Claude Code) and an MCP resource `exomem://adoption/run/<id>` in `resources/list` with `list_changed` on creation. The `handoff` block also carries a `claude://` prefill link and per-CLI one-liners as `links`. Nothing is built on dead mechanisms (web `claude.ai/new?q=`, MCP sampling, ChatGPT auto-submit).

### 8. Hosted entrypoint

`adoption_studio` is admitted by the generic hosted command route: `invocation_is_read_only(command, kwargs)` classifies `status`/`work-item` as reads (`admit_read()`) and every other action as a mutation (`admit_mutation()`). **Do NOT add `adoption_studio` to the hosted intercept set** (which stays `transfer_artifact` / `adopt_vault`); `resolve_under_vault` path confinement plus the run state machine is the safety layer. Zero hosted-route change for command flow.

Uploads/ZIPs (thin cell-side entrypoint in `hosted_transfer_routes.py`): land RAW files under vault-relative `_Staging/adoption/<run_id>/` (outside `Knowledge Base/` so the engine treats them as legacy input). ZIPs are expanded cell-side with **zip-slip protection** (every extracted path confined under the staging dir via `resolve_under_vault`) and enforced **entry-count and total-size caps**. `adoption_studio(action="start", path="_Staging/adoption/<run_id>")` scans staged material with the identical engine used locally; local runs never use staging. The engine keeps its four host-neutral properties: injected `vault_root`, vault-relative paths, `Knowledge Base/`-only writes, registry command. Lane D touches `hosted_transfer_routes.py` + its tests only.

### 9. Golden schema regeneration

`tests/fixtures/mcp_tool_schemas.json` changes for exactly four tools: `adoption_studio` (new), `review_memory`, `triage_memory`, `review_item_context` (docstring updates). Procedure: (1) land code + red tests, run `tests/test_mcp_schema_fidelity.py`, confirm the failure names exactly those four (proves no accidental drift); (2) regenerate with the test's own `_build_server` / `_live_schemas` helpers (`json.dumps(live, indent=2, ensure_ascii=False)`, order-sensitive); (3) commit the fixture in the same PR with an explicit intentional-change note; re-run green.

### 10. UI architecture digest

Same Studio app, new top-level view `?view=adopt` (not a sibling page — the asset route refuses `index.html` aliases). Immutable caching forces filename bumps: `app.v3.js→app.v4.js`, `state.v1.js→state.v2.js`, `styles.v1.css→styles.v2.css`, new `adoption.v1.js` (controller + the single `adoptionApi` adapter object) + `adoption-model.v1.js` (pure, node-testable), `manifest.json` version 4. CSP forbids inline scripts/styles; shipped assets must stay inert — the handoff `prompt_text` and any links come from the API at runtime, never baked into JS.

Route model (`state.v2.js`): `view ∈ {review, adopt}`, `run` (opaque id), `astep ∈ {start, findings, choose, preview, organize, suggestions, question}`. `writeRoute` emits nothing for review defaults so every legacy review URL serializes byte-identically. Only reviewable steps are routable; scanning/applying are phase-driven. The server phase always wins (`legalStep(phase, astep)` snaps to the nearest legal screen).

Nine screens: Start (folder input + resume card) → Scanning (read-only, cancellable) → Findings ("Here's what we found": tiles, pack suggestions as plain phrases, junk demoted, non-text explained) → Choose (folder-level tri-state rules + per-file overrides; server materializes; 200-row caps with honest count lines) → Preview ("Exactly what will happen" contract card + per-item disclosure + confirm dialog) → Applying/Result (progress, partial-failure groups with coded plain reasons, post-apply checksum verification line from `verified_unchanged`/`verified_total`) → Organize handoff (optional; copyable server-provided `prompt_text`; 120 s "may not be connected" notice; skippable) → Review suggestions (reuses the review-workspace idiom, per-item approve/reject, drift-refresh) → First question (deterministic chips → ask → done).

**UI↔engine action mapping (reconciliation):** the UI "Preview" screen consumes the engine `plan` action (exact-action preview). The UI adapter's assumed `preview`/`approve-proposal`/`create`/`retry` names are internal to `adoptionApi`; the real product actions are the ten in Decision 1 (`plan` for preview, `apply-proposal` for approve, `start` for create, `apply(retry_failed=true)` / `scope:"failed"` for retry). Reassurance mechanics: persistent guarantee badge on every step, per-step contract line above each primary button, confirm dialog restating numbers, honest verification line, every terminal error/cancel ending with the originals-status line. Polling via `setTimeout` chains (1.5 s scan/apply backing to 4 s after 60 s; 5 s proposals to 15 s after a 120 s timeout notice); pause on `document.hidden`.

## Substrate Home integration spec

Written so a separate team implements Home with no adoption design work. Substrate is read-only for this change; nothing here is implemented in the exomem repo.

- **Transport:** Home → `POST /api/exomem/commands/adoption_studio` proxy → `routeExomemCommand` (`src/lib/exomem-hosted/gateway.ts`) → `POST <cell>/private/exomem/v1/command/adoption_studio`. `adoption_studio` is a normal registry command; it is NOT in `gateway.ts` `INTERCEPTED_COMMANDS` (which stays `{"transfer_artifact","adopt_vault"}`) and needs no `commandInterceptRequired()` handling.
- **Contract pin bump:** the pinned `gateway-contract-<ver>.ts` (sha256-digest-verified) picks up `adoption_studio` automatically once the cell version bumps; the spec's acceptance requires updating the pinned contract file and its digest to include the new command (21 → 22 commands).
- **Payload shapes (proxied verbatim to the cell command):** `start {action:"start", path, initialize_kb?}` → `{run_id, phase}`; `status {action:"status", run_id}` → full run doc incl. `phase`, `staleness`, `handoff`; `select {action:"select", run_id, include, exclude, overrides, include_junk}`; `plan {action:"plan", run_id}` → exact-action preview; `apply {action:"apply", run_id, plan_id}` → `applying` then `apply_result` incl. `verified_unchanged`/`verified_total`; `cancel {action:"cancel", run_id, why}`; `apply(retry_failed=true, only_paths)` for `scope:"failed"`; `finish {action:"finish", run_id}`. Proposals ride `review_memory {mode:"adoption", ref?}`, `review_item_context {ref, expected_fingerprint}`, `triage_memory {ref, action:"dismiss", …}`, and approval `adoption_studio {action:"apply-proposal", ref, expected_fingerprint, why}`. Stale drift returns HTTP 409 `ADOPTION_SOURCE_CHANGED` (preview/apply) or `REVIEW_ITEM_CHANGED` (proposal).
- **Staging upload route:** a dedicated `src/app/api/exomem/adopt/upload/route.ts` modeled on the existing `upload/route.ts` + `transfers.ts` (bound transfer grants, HMAC claims, per-tenant `uploadBytes` cap) that streams multi-file / ZIP intake to the cell's `_Staging/adoption/<run_id>/` landing; multi-entry/folder/ZIP is the net-new extension over today's single-file upload. ZIP expansion, zip-slip protection, and entry/size caps are enforced cell-side (Decision 8), not in Home.
- **UI mounting:** new sibling route `src/app/exomem/adopt/page.tsx` under `PrivateShell` (mirrors `invite/`, `delete/`); client command calls added to `hosted-browser.ts`; auth/CSRF/idempotency via `sessions.ts`; first recall via `commands/ask_memory`.
- **Poll cadence reuse:** the adoption run is a cell-owned job Home polls via the `status` registry command, reusing the `home-state.ts` single-flight status-poll (3 s → 30 s exponential) — NOT the provisioner `durability.ts` operation queue (that is verb-bound).
- **Acceptance tests (Substrate, node:test):** proxy forwards `adoption_studio` without interception; contract-pin digest updated and verified; upload route lands multi-file/ZIP into per-run staging and rejects oversize/over-count; `page.tsx` drives start → status-poll → preview → apply → verify line; proposals list/approve/reject via the review verbs; a browser harness is named as net-new and out of scope for the exomem change.

## Risks / Trade-offs

- Folding the imported-copy write into `commit_import_items` must not change `_copy_as_sources`'s external result shape; the existing `tests/test_adopt.py` suite is the guardrail.
- Selection is last-write-wins under the lease (plan/apply are already guarded by `plan_id` + `selection_hash`); `expected_rev` CAS on `select` is a noted follow-on if multi-client editing ships.
- The golden fixture is the one hard-to-change surface; regeneration must name exactly the four intended tools or the gate is hiding drift.

## Open Questions

None blocking. Deferred: `expected_rev` CAS on `select` for concurrent multi-client editing; richer non-text import kinds (this import copies text/markdown-like files only).
