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
      proposal_id; commit reserves for one event and consumes once on activation;
      exact-prior crash releases reservation; re-commit after success is refused;
      commit refuses on policy/item fingerprint drift
      (`STALE_GOVERNANCE_POLICY`).
- [ ] 2.2 Implement `proposals` table (TTL, fingerprint_at_propose,
      affected_item_fingerprints) in `store.py`; reserve through the uniquely
      bound pending operation journal under `BEGIN IMMEDIATE`, leaving the
      proposal row logically unspent until activation marks it spent; prepare
      validated YAML + prior-version archive + target fingerprint;
      `undo` inverse. State activation is completed under section 4's receipt
      protocol.

## 3. grant / revoke / suspend / resume / declare

- [ ] 3.1 Red test: grant redeems a withhold-token → session grant the lattice
      sees; second use refused; `revoke(scope=session)` clears all; `declare`
      sets session purpose; suspend/resume toggle a rule-set. Inject crashes
      between both child intents, the compound token-consume/pending-grant SQLite
      commit, both terminals, and activation; approval is never consumed without
      its exact recoverable pending grant.
- [ ] 3.2 Implement `session_grants`/`session_purpose` tables (TTL sweeps); grant
      bounded by token; consume token + create pending grant in one
      `BEGIN IMMEDIATE`; `undo` re-resolves dependent grants and expires drifted
      ones.

## 4. Receipt-first activation and recovery

- [ ] 4.1 Red tests map every authorization-affecting operation (`commit`,
      `grant`, `revoke`, `suspend`, `resume`, `undo`, `declare`) to a distinct
      receipt event/recovery policy; prove token redemption and grant creation
      are causally linked but distinct children of one atomic sidecar transition,
      and prove adding an unmapped operation fails coverage. Exercise both
      widening and narrowing forms of `commit`, `suspend`, `resume`, `undo`, and
      `declare`; direction comes from the effective before/after lattice and
      unknown is widening. `propose` is the explicit non-authorizing exemption.
- [ ] 4.2 Red crash-injection tests at intent, pending-state, target-write,
      terminal, and activation boundaries. Cover warm last-good, cold BLOCKED,
      exact-prior abort, exact-prepared completion, final-active recognition only
      with terminals, partial/third-state
      blocking, incomplete compound-grant children, undo YAML present with stale
      dependent grant, intent + pending journal before any target write, and a
      YAML-only prepared→final-active transition distinguished by the pending
      journal and phase-domain, with idempotent retry and no semantic mutation
      replay.
- [ ] 4.3 Implement the reserved plaintext-free pending marker in
      `governance/policy.py`/`governance/tool.py`: durable intent first; fsync
      policy files/directories; durable terminal; remove/directory-fsync the
      marker while the journal still blocks; atomically verify/finalize sidecar
      rows and close the journal last.
      Prior/prepared/final-active composite digests cover every affected YAML
      path and authorization-state sidecar row/status, including proposal
      consumption and dependent grants. Exclude the self-referential operation
      journal and marker as separately validated control metadata. Manual YAML
      behavior remains unchanged when no guard exists.
- [ ] 4.4 Add prepared/final authorization rows plus pending/closed operation
      journals keyed by event id. All target rows are invisible until terminal
      evidence; only a separately proven narrowing overlay may fail closed
      earlier. Make the pending journal authoritative across marker removal,
      finalize all sidecar rows atomically, verify final-active and close the
      journal in that transaction, reconcile all open composite states before
      later authoring writes, and never replay the semantic mutation. Closed
      journals are excluded from live comparison.
- [ ] 4.5 Make `governance/store.py` the sole migration owner for monotonic
      v2→v3; test every receipt/token/policy/tool opener preserves v3 and later.
- [ ] 4.6 Make every token/proposal/grant/purpose TTL or GC sweep pin rows named
      by a pending journal. Red tests advance past TTL and sweep (a) exact
      prepared state, which remains recoverable but non-authorizing if expired,
      and (b) closed final-active state, whose later legitimate row deletion does
      not block subsequent authoring.

## 5. Registration + destructive annotation (only after section 4 passes)

- [ ] 5.1 Add `_PRODUCT_SPEC` entry (Tier-2, writes=True) + `DESTRUCTIVE_OPS`
      membership for policy-overwriting ops; regenerate
      `tool_surface_contract.json` via `scripts/dump-tool-schemas.py`.
- [ ] 5.2 Update `test_mcp_schema_fidelity.py`/`test_tool_surface_contract.py`/
      `test_connector_guardrails.py` pins; CLI + REST smoke
      (`kb govern_memory list --json`).

## 6. Bootstrap + teaching surface

- [ ] 6.1 Red test `tests/test_bootstrap.py::test_bootstrap_reports_governance`;
      add the `governance` payload section + bump `contract_version` in
      `op_bootstrap`.
- [ ] 6.2 Add generic `_scaffold/_Schema/references/governance.md` + `SKILL.md`
      section (lifecycle + forged-envelope rule); `tests/test_scaffold_no_leak.py`
      green.

## 7. Gates

- [ ] 7.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_govern_memory_tool.py tests/test_bootstrap.py
      tests/test_mcp_schema_fidelity.py tests/test_tool_surface_contract.py
      tests/test_connector_guardrails.py tests/test_scaffold_no_leak.py` green.
- [ ] 7.2 Full suite and `uv run python -m pytest tests/test_latency_gate.py -q`
      green.
- [ ] 7.3 `uvx ruff check` clean; `openspec validate add-governance-tools
      --strict` green.
