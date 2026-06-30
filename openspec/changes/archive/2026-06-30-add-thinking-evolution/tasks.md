# Tasks — Thinking-evolution view (`evolution`)

## 1. Pointer reader + pure builder (TDD: tests before wiring)
- [x] 1.1 Add `ParsedPage.supersedes` to `src/kb_mcp/find.py` (mirrors `.superseded_by`:
      returns the `supersedes` frontmatter wikilink(s) as a list, empty when absent).
- [x] 1.2 Write `tests/test_evolution.py` FIRST over a tmp-vault, torch-free: a 3-version
      chain A→B→C (both pointers set + banners/reasons) → one timeline ordered [A,B,C], each
      with structural claims, transitions A→B and B→C carrying recorded reasons, head C
      transition null, span {from,to,n_versions=3}; a never-superseded note → no timeline;
      two hits on the same chain → one timeline (dedup); two separate chains → two timelines;
      timeline cap → `truncation` entry; ordering by pointer spine even when dates disagree;
      determinism on re-run; empty result when no supersession matches.
- [x] 1.3 Implement `src/kb_mcp/evolution.py`: env-resolved caps (`KB_MCP_EVOLUTION_MAX_CHAINS`
      default 10, `KB_MCP_EVOLUTION_MAX_VERSIONS` default 25); `Version`/`Timeline` dataclasses
      with `as_dict()`; `_resolve_chain(vault_root, page)` (walk `superseded_by`/`supersedes`
      via `find._CACHE`, `seen`-guarded), `_order_chain` (origin→head spine), `_transition_reason`
      (banner `Reason:` / `get` log `why:`); pure `build_timelines(vault_root, hits, *, …)`
      reusing `context_pack._extract_claims`; public `evolution(vault_root, *, query, limit=10,
      scope="kb", projects=None, tags=None)` that calls `find()` (overfetch) then builds. No
      mutation, no model import.
- [x] 1.4 `tests/test_evolution.py` green.

## 2. Registry wiring (all surfaces from one entry)
- [x] 2.1 Add `op_evolution(vault_root, query="", limit=10, scope="kb", projects=None,
      tags=None) -> dict` to `commands.py` with the load-bearing Google-style docstring
      (lead: "how a conclusion CHANGED over time / its supersession history"; defers plain
      lookup to `find`; documents the timelines/versions/transition shape). Import
      `from . import evolution as evolution_module`.
- [x] 2.2 Add the `_SPEC` line `("evolution", op_evolution, 1, False, False, "query", _MCRC)`
      after `attention`. No `HAND_REGISTERED_EXCEPTIONS` change (registry-generated).
- [x] 2.3 Assert `evolution` on MCP (`test_mcp_schema_fidelity`), REST + OpenAPI params
      (`test_rest_registry`), CLI (`test_cli_core_ops`), and an e2e call
      (`test_consolidated_tools`); derived params exactly `{query, limit, scope, projects, tags}`.

## 3. Schema-fidelity fixture
- [x] 3.1 Regenerate `tests/fixtures/mcp_tool_schemas.json` via `scripts/dump-tool-schemas.py`;
      confirm the diff adds only the `evolution` tool and changes no existing tool.

## 4. Verify
- [x] 4.1 `uv run pytest tests/test_evolution.py tests/test_mcp_schema_fidelity.py
      tests/test_rest_registry.py tests/test_consolidated_tools.py tests/test_cli_core_ops.py -q`
      green.
- [x] 4.2 Full suite via `uv run pytest -q` — 831 passed, 7 skipped, 0 errors (collects clean;
      the torch CI fix is on main). +9 evolution tests, no regression.
- [x] 4.3 `ruff check` clean on `src/kb_mcp/evolution.py` + `src/kb_mcp/find.py` +
      `src/kb_mcp/commands.py`.
- [x] 4.4 CLI smoke (fixture vault): `kb evolution "<topic>" --json` returns timelines (or an
      explicit empty result with a note) — pick a fixture topic with a supersession chain, add
      one to the fixture vault if none exists.
- [x] 4.5 Pure-substrate check: `evolution.py` imports no model/embedding module for generation;
      claims/reasons are verbatim note text; vault + `find` ordering unchanged.
- [x] 4.6 `openspec validate add-thinking-evolution --strict` passes.

## 5. Deploy (Hugo)
- [ ] 5.1 `reset --hard origin/main` (or ff-pull) on the deploy checkout + restart; reconnect
      the claude.ai connector so the new `evolution` MCP tool appears. (Additive, read-only.)
