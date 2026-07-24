# Tasks: add-release-gate

## 1. Principal / audience (src/exomem/governance/principal.py)

- [ ] 1.1 Red test `tests/test_governance_principal.py`: per-surface resolver
      (fake FastMCP deps via monkeypatch; REST scope passthrough; owner default);
      unresolved-but-expected fails closed to most-restrictive.
- [ ] 1.2 Implement `RequestPrincipal` + contextvar + `request_scope()` (clone of
      `capabilities.active_surface`); set-points in `bind_vault` wrapper
      (`command_surface.py:211`, beside `mcp_retry_scope`), `server_rest.py:267`,
      hosted invoke wrapper, `__main__.py:2037`; one canonical-audience normalizer.

## 2. Projector + decision annotation (egress.py, find_types.py)

- [ ] 2.1 Red test `tests/test_governance_egress.py::test_op_find_annotates_and_filters`:
      withheld hit → notice; permitted hit → decision; pack excludes withheld;
      graph seed / relation_match naming withheld path scrubbed.
- [ ] 2.2 Implement `project(payload, level)` per-level allow-list as the only
      serializer; add `decision` to `Hit`/`SemanticUnitHit`; remove raw
      serializers from the egress path; unregistered-projector fail-closed.
- [ ] 2.3 Insert `annotate_hits` at `commands.py:901→902` (after `find()`, before
      `assemble_pack`); request-deterministic backfill from an over-fetch pool;
      L0 silent at pool exhaustion.

## 3. Cache invariant

- [ ] 3.1 Red test `test_governance_egress.py::test_find_hot_cache_stays_principal_free`
      (two audiences; cached second call; cached copies carry `decision=None`);
      separate decision memo keyed `(fingerprint, path, audience, purpose,
      grants-hash)`. No production change to `_FIND_CACHE` key expected.

## 4. get / graph / packs / purpose param

- [ ] 4.1 Red tests `test_get_respects_decision_levels`,
      `test_graph_context_guard_seed_governed`, pack-decision test.
- [ ] 4.2 `annotate_page` in `op_get` (`commands.py:1838+`); `guard_seed` in
      `op_graph_context`/`epistemic_graph`; pack elements carry decisions.
- [ ] 4.3 Add `purpose` param to `op_find`/`op_ask_memory`/`op_read_memory`/
      `op_graph_context`; regenerate `tool_surface_contract.json` via
      `scripts/dump-tool-schemas.py`; update `test_mcp_schema_fidelity.py` /
      `test_tool_surface_contract.py` / `test_connector_guardrails.py` pins.

## 5. Withhold-tokens (tokens.py)

- [ ] 5.1 Red test `tests/test_governance_tokens.py`: mint/verify/expire/
      single-use-redeem/sweep; content-fingerprint binding; drift refuses.
- [ ] 5.2 Implement `wh1.` token format, per-machine sidecar HMAC key,
      `BEGIN IMMEDIATE` consume-once; withheld notices embed tokens.

## 6. Terminal scrubber + postfilter (scrubber.py, egress.postfilter)

- [ ] 6.1 Red test `tests/test_governance_postfilter.py`: credential patterns
      (keys/JWT/high-entropy); no false positive on `content_hash`/`ref` fields;
      withheld-path cross-check; `ToolResult` text-block-only handling.
- [ ] 6.2 Implement `scrubber.py` (always-on) + `egress.postfilter`; call in
      `writer_lease.invoke_command` (primary) + `bind_vault` (MCP second pass);
      explicit calls at adoption tools + transfer routes; startup assertion that
      every product command resolves to a projector-registered leaf.
- [ ] 6.3 Red tests `test_release_gate_runs_on_rest_surface`,
      `test_release_gate_runs_on_cli_surface`,
      `test_retrieve_inject_hook_respects_release` (surface parity).

## 7. Transfer / media gating

- [ ] 7.1 Red tests `test_transfer_download_denies_below_l6`,
      `test_read_media_frames_gated`.
- [ ] 7.2 `op_transfer_artifact` download-target selection and
      `op_get_video_frames` consult the release decision before minting/returning.

## 8. Gates

- [ ] 8.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_governance_egress.py tests/test_governance_principal.py
      tests/test_governance_tokens.py tests/test_governance_postfilter.py
      tests/test_find.py tests/test_context_pack.py` green.
- [ ] 8.2 New `tests/test_governance_overhead.py`: `op_find` empty-policy delta
      < 5 ms median over `gen_dense_vault` 2k; scrubber < 2 ms per 100 KB.
- [ ] 8.3 `uv run python -m pytest tests/test_latency_gate.py -q` +
      `tests/test_retrieval_golden.py` (embeddings lane) green — thresholds and
      goldens unchanged.
- [ ] 8.4 `uvx ruff check` clean; `openspec validate add-release-gate --strict`
      green.
