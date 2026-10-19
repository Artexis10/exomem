# Tasks — Attention queue (unified review surface)

## 1. Pure ranker (TDD: tests before wiring)
- [x] 1.1 Write `tests/test_attention.py` FIRST over synthetic `AuditFinding` lists feeding
      `attention._rank` (torch-free, no vault): rank-major interleave at equal weights;
      multi-signal boost + dedup (one item, both reasons, score == sum, ranks above
      single-flagged, severity == max); contradiction anchor + pair preserved (`item.path==A`,
      reason `related_paths==[A,B]`, `B` not independent unless separately flagged);
      truncation (`limit` cap → `shown/total/truncated` + `note`; large `limit` → no `note`);
      `limit<=0` uncapped; empty findings; same-category double-anchor (best-rank score);
      upstream folding (synthetic summary finding → not an item, `upstream_truncated==N`, note
      clause); categories filter + `ValueError` on bogus; severity precedence + byte-identical
      determinism on re-run.
- [x] 1.2 Implement `src/kb_mcp/attention.py`: `ATTENTION_CATEGORIES`, `_CATEGORY_ORDER`,
      `_SEVERITY_RANK`, default `_WEIGHTS`; `AttentionItem` / `AttentionReport` dataclasses with
      `as_dict()` (omit-when-empty like `AuditFinding`); pure
      `_rank(findings, *, categories, limit, weights) -> AttentionReport` reusing
      `fusion.reciprocal_rank_fusion_weighted`; public
      `attention(vault_root, *, categories=None, limit=25, today=None) -> AttentionReport`
      that validates `categories ⊆ ATTENTION_CATEGORIES` and calls `audit()` once.
- [x] 1.3 `tests/test_attention.py` green (16 tests).

## 2. Registry wiring (all surfaces from one entry)
- [x] 2.1 Add `op_attention(vault_root, categories=None, limit=25) -> dict` to `commands.py`
      next to `op_audit`, with the load-bearing Google-style docstring (review-queue framing +
      defer-to-`audit` line + Args/Returns; documents `limit<=0` = uncapped). Import
      `from . import attention as attention_module`.
- [x] 2.2 Add `("attention", op_attention, 1, False, False, None, _MCRC)` to `_SPEC` after
      `audit`. No `HAND_REGISTERED_EXCEPTIONS` change (registry-generated).
- [x] 2.3 Assert `attention` on MCP (`test_mcp_schema_fidelity`), REST + OpenAPI params
      (`test_rest_registry::test_attention_route_and_openapi_params`), CLI
      (`test_cli_core_ops::test_attention_runs`), and an e2e MCP call
      (`test_consolidated_tools::test_attention_tool_composes_review_surface`); derived params
      exactly `{categories, limit}`.

## 3. Schema-fidelity fixture
- [x] 3.1 Add `scripts/dump-tool-schemas.py` — reusable regenerator that serializes the live MCP
      tool schemas via the same path `tests/test_mcp_schema_fidelity.py` uses, writing
      `tests/fixtures/mcp_tool_schemas.json` (top-level keys sorted; nested order preserved).
- [x] 3.2 Regenerate the fixture to include the `attention` tool; diff adds only `attention`
      (+29 lines), changes no existing tool's schema/description.

## 4. Verify
- [x] 4.1 `uv run pytest tests/test_attention.py tests/test_mcp_schema_fidelity.py
      tests/test_rest_registry.py tests/test_consolidated_tools.py tests/test_cli_core_ops.py -q`
      green (57 passed).
- [x] 4.2 Full suite via `pytest -q` — 863 passed, 1 skipped (pre-existing diarizer-sidecar
      smoke; no regression).
- [x] 4.3 `ruff check` clean on the changed files.
- [x] 4.4 CLI smoke (fixture vault): `kb attention --limit 5 --json` and
      `kb attention --categories stale_review --json` return one ranked envelope; the live-vault
      smoke is the operator's at deploy.
- [x] 4.5 Pure-substrate check: `attention.py` imports only `audit` + `fusion` (no
      embedding/model module); `_rank` reads only pre-computed `AuditFinding` fields; `find`
      output unchanged.
- [x] 4.6 `openspec validate add-attention-queue --strict` passes.

## 5. Deploy (operator)
- [ ] 5.1 `reset --hard origin/main` on the deploy checkout + restart; reconnect the claude.ai
      connector so the new `attention` MCP tool appears. (Additive, read-only — safe.)
