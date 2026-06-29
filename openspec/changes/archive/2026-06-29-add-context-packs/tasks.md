# Tasks — Reasoning-ready context packs from `find`

## 1. Pure assembler (TDD: tests before wiring)
- [x] 1.1 Write `tests/test_context_pack.py` FIRST over a small synthetic vault cluster
      (inter-linked notes + one supersession pair), torch-free (embeddings disabled):
      claim extraction (lede skips H1, recognized `## Summary`/`## Problem` section lines,
      `##` outline, code-fence `#`/`[[...]]` ignored, claim chars capped); neighbourhood
      (inbound+outbound union, packed notes excluded, `direction` in/out/both, co-citation
      order — 2-cited above 1-cited, neighbour cap → `truncation` entry, one-sentence
      ledes); supersession edges read from frontmatter among the set; embeddings-off
      degradation (`embeddings_available==false`, `tension==[]`, other parts populated);
      tension-band membership via injected cosines (a stub/monkeypatched
      `_best_cosine_per_file` returning band/above-band/below-band scores → only band
      pairs surface, framed proximity-not-polarity); bounded hits (`packed_paths` ==
      top-N); determinism on re-run.
- [x] 1.2 Implement `src/kb_mcp/context_pack.py`: env-resolved caps
      (`KB_MCP_PACK_MAX_HITS=5`, `KB_MCP_PACK_MAX_NEIGHBORS=10`, `KB_MCP_PACK_MAX_TENSION=10`,
      `KB_MCP_PACK_CLAIM_CHARS=280`), `RECOGNIZED_SECTIONS` set; helpers `_extract_claims`,
      `_neighborhood`, `_contradictions`, fence-aware heading/lede scan; pure
      `assemble_pack(vault_root, hits, *, max_hits=None, max_neighbors=None,
      max_tension=None) -> dict` reusing `find._CACHE`/`_outbound_wikilink_paths`,
      `vault.find_inbound_wikilinks`, `corpus_aware._best_cosine_per_file` +
      `_contradiction_floor`/`_dup_threshold` + `_canon`. No mutation, no model import.
- [x] 1.3 `tests/test_context_pack.py` green.

## 2. Wire `pack` into `find` (leaf only)
- [x] 2.1 Add `pack: bool = False` to `op_find` in `commands.py` with an Args docstring
      entry (assemble a reasoning-ready context pack from the top hits; measurement-only;
      list-vs-`{hits,pack}` return) and a `Returns:` note for the pack shape. Import
      `from . import context_pack as context_pack_module`.
- [x] 2.2 Branch the return: `pack=false` → `[h.as_dict() for h in hits]` (unchanged);
      `pack=true` → `{"hits": [...], "pack": context_pack.assemble_pack(vault_root, hits)}`.
      No `_SPEC` edit (param auto-derived); no `HAND_REGISTERED_EXCEPTIONS` change.
- [x] 2.3 Integration test: `op_find(vault, query=..., pack=true)` returns the
      `{"hits","pack"}` shape with a sane pack; `pack` absent/false returns the bare list
      byte-identical to before (add to `tests/test_find.py` or `tests/test_consolidated_tools.py`).

## 3. Schema-fidelity fixture
- [x] 3.1 Regenerate `tests/fixtures/mcp_tool_schemas.json` via
      `scripts/dump-tool-schemas.py`; confirm the diff adds only `find`'s `pack` boolean
      property (+ its description) and changes no other tool's schema/description.

## 4. Verify
- [x] 4.1 `uv run pytest tests/test_context_pack.py tests/test_find.py
      tests/test_mcp_schema_fidelity.py tests/test_consolidated_tools.py
      tests/test_rest_registry.py tests/test_cli_core_ops.py -q` green.
- [x] 4.2 Full suite via `uv run pytest -q` — 814 passed, 6 skipped, no regression. (1
      pre-existing collection error: `tests/test_voice_embed.py` bare `import torch`, the
      worktree venv lacks the embeddings/torch extra; unrelated — this change is torch-free.)
- [x] 4.3 `ruff check` clean on `src/kb_mcp/context_pack.py` + `src/kb_mcp/commands.py`.
- [x] 4.4 CLI smoke (fixture vault): `kb find "<term>" --pack --json` returns an envelope
      whose `data` has `hits` + `pack`; `kb find "<term>" --json` still returns the list.
- [x] 4.5 Pure-substrate check: `context_pack.py` imports no embedding/model module for
      generation (only `corpus_aware`'s precomputed-cosine helper); assembly reads content
      + frontmatter + wikilinks only; vault and `find` ordering unchanged.
- [x] 4.6 `openspec validate add-context-packs --strict` passes.

## 5. Deploy (Hugo)
- [ ] 5.1 `reset --hard origin/main` on the deploy checkout + restart; reconnect the
      claude.ai connector so `find`'s new `pack` param appears. Live smoke: one real
      `find(pack=true)` confirms `tension` pairs populate from the band and the pack is
      token-bounded. (Additive, read-only — safe.)
