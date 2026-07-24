# Tasks: add-governance-tools

## 1. Tool router skeleton (src/exomem/governance/tool.py)

- [ ] 1.1 Red test `tests/test_govern_memory_tool.py`: `list`/`explain`/`simulate`
      classify read-only via `invocation_is_read_only`; write ops classify
      writing.
- [ ] 1.2 Implement `op_govern_memory(vault_root, operation, ...)` router +
      `_GOVERN_MEMORY_READ_ONLY_ACTIONS` in `commands.py:5442`.

## 2. propose / commit with drift-bound nonce

- [ ] 2.1 Red test: propose returns interpretation + canonical YAML + membership
      preview (rendered at current ceiling, no restricted titles) + consequences +
      proposal_id; commit consumes once; re-commit refused; commit refuses on
      policy/item fingerprint drift (`STALE_GOVERNANCE_POLICY`).
- [ ] 2.2 Implement `proposals` table (TTL, fingerprint_at_propose,
      affected_item_fingerprints) in `store.py`; commit under `BEGIN IMMEDIATE`;
      atomic YAML write + prior-version archive + fingerprint bump; `undo` inverse.

## 3. grant / revoke / suspend / resume / declare

- [ ] 3.1 Red test: grant redeems a withhold-token → session grant the lattice
      sees; second use refused; `revoke(scope=session)` clears all; `declare`
      sets session purpose; suspend/resume toggle a rule-set.
- [ ] 3.2 Implement `session_grants`/`session_purpose` tables (TTL sweeps); grant
      bounded by token; `undo` re-resolves dependent grants and expires drifted
      ones.

## 4. Registration + destructive annotation

- [ ] 4.1 Add `_PRODUCT_SPEC` entry (Tier-2, writes=True) + `DESTRUCTIVE_OPS`
      membership for policy-overwriting ops; regenerate
      `tool_surface_contract.json` via `scripts/dump-tool-schemas.py`.
- [ ] 4.2 Update `test_mcp_schema_fidelity.py`/`test_tool_surface_contract.py`/
      `test_connector_guardrails.py` pins; CLI + REST smoke
      (`kb govern_memory list --json`).

## 5. Bootstrap + teaching surface

- [ ] 5.1 Red test `tests/test_bootstrap.py::test_bootstrap_reports_governance`;
      add the `governance` payload section + bump `contract_version` in
      `op_bootstrap`.
- [ ] 5.2 Add generic `_scaffold/_Schema/references/governance.md` + `SKILL.md`
      section (lifecycle + forged-envelope rule); `tests/test_scaffold_no_leak.py`
      green.

## 6. Gates

- [ ] 6.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_govern_memory_tool.py tests/test_bootstrap.py
      tests/test_mcp_schema_fidelity.py tests/test_tool_surface_contract.py
      tests/test_connector_guardrails.py tests/test_scaffold_no_leak.py` green.
- [ ] 6.2 `uv run python -m pytest tests/test_latency_gate.py -q` green.
- [ ] 6.3 `uvx ruff check` clean; `openspec validate add-governance-tools
      --strict` green.
