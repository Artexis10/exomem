# Tasks: add-governance-tools

## 1. Tool router skeleton (src/exomem/governance/tool.py)

- [x] 1.1 Red test `tests/test_govern_memory_tool.py`: `list`/`explain`/`simulate`
      classify read-only via `invocation_is_read_only`; write ops classify
      writing.
- [x] 1.2 Implement `op_govern_memory(vault_root, operation, ...)` router +
      `_GOVERN_MEMORY_READ_ONLY_ACTIONS` in `commands.py:5442`.

## 2. propose / commit with drift-bound nonce

- [x] 2.1 RED/repair tests: client/LLM supplies canonical documents and intent;
      no server NLP/planning; samples/privacy/consequences/overlaps derive only
      from current/prospective compiled policy and concrete membership; selector
      and ceiling hints mismatch diagnostically or refuse without authority.
- [x] 2.2 Repair `proposals` table (TTL, fingerprint_at_propose,
      affected_item_fingerprints) in `store.py`; reserve through the uniquely
      bound pending operation journal under `BEGIN IMMEDIATE`, leaving the
      proposal row logically unspent until activation marks it spent; prepare
      validated YAML + prior-version archive + target fingerprint;
      `undo` inverse. Verify commit identity and direction derive from compiled
      policy/membership, not caller hints; state activation follows section 4.

## 3. grant / revoke / suspend / resume / declare

- [x] 3.1 RED/repair tests: grant redeems a withhold-token → session grant the lattice
      sees; second use refused; `revoke(scope=session)` clears all; `declare`
      stages a purpose while its prior active value remains visible; suspend/resume
      toggle a rule-set. Inject crashes
      between both child intents, the compound token-consume/pending-grant SQLite
      commit, both terminals, and activation; approval is never consumed without
      its exact recoverable pending grant.
- [x] 3.2 Repair `session_grants`/`session_purpose` tables (TTL sweeps); grant
      bounded by token; consume token + create pending grant in one
      `BEGIN IMMEDIATE`; make purpose staging event-keyed and atomically
      promote/delete it at activation; `undo` re-resolves dependent grants and
      expires drifted ones.

## 4. Receipt-first activation and recovery

- [x] 4.1 RED/repair tests map every authorization-affecting operation (`commit`,
      `grant`, `revoke`, `suspend`, `resume`, `undo`, `declare`) to a distinct
      receipt event/recovery policy; prove token redemption and grant creation
      are causally linked but distinct children of one atomic sidecar transition,
      and prove adding an unmapped operation fails coverage. Exercise both
      widening and narrowing forms of `commit`, `suspend`, `resume`, `undo`, and
      `declare`; direction comes from the effective before/after lattice and
      unknown is widening. `propose` is the explicit non-authorizing exemption.
- [x] 4.2 RED/repair crash-injection tests at allocating, intent, arm-pending,
      marker/target-write, terminal, and activation boundaries.
      Cover warm last-good, cold BLOCKED, allocating exact-prior close that aborts
      only observed intents and never invents a terminal, exact prepared
      completion, final-active recognition only with terminals, partial/third-state
      blocking, incomplete compound-grant children, undo YAML present with stale
      dependent grant, and idempotent retry with no semantic mutation replay.
- [x] 4.3 Repair the two-stage journal and exact marker in
      `governance/policy.py`/`governance/tool.py`: durable `allocating` ignored by
      policy reads; fsync actual intents; atomically arm `pending`; only then
      prepare marker/target. Require a regular non-symlink marker binding
      protocol/phase/event/operation/all digests/affected ids/canonical sorted
      paths, with absence only in exact-prior-before-create or terminal-backed
      removal; directory-fsync marker removal while the journal still blocks.
      Prior/prepared/final-active composite digests cover every affected YAML
      path and authorization-state sidecar row/status, including proposal
      consumption and dependent grants. Exclude the self-referential operation
      journal and marker as separately validated control metadata. Manual YAML
      behavior remains unchanged when no guard exists.
- [x] 4.4 Repair prepared/final authorization rows plus pending/closed operation
      journals keyed by event id. All target rows are invisible until terminal
      evidence; only a separately proven narrowing overlay may fail closed earlier.
      Make pending authoritative across marker removal, atomically finalize sidecar
      rows and staged purpose, verify final-active and close; reconcile exact
      prior/prepared/final without semantic replay. Closed journals are excluded
      from live comparison.
- [x] 4.7 Verify explicit audience handling: explain resolves one exact canonical
      path, simulate resolves explicit canonical paths, and neither silently
      ignores audience behavior.
- [x] 4.8 Verify full versioned composite coverage and pointwise lattice direction:
      every authorization-bearing field binds prior/prepared/final, and incomplete
      proof is conservative widening.
- [x] 4.5 Make `governance/store.py` the sole migration owner for monotonic
      v2→v3; test every receipt/token/policy/tool opener preserves v3 and later.
- [x] 4.6 Make every token/proposal/grant/purpose TTL or GC sweep pin rows named
      by a pending journal. Red tests advance past TTL and sweep (a) exact
      prepared state, which remains recoverable but non-authorizing if expired,
      and (b) closed final-active state, whose later legitimate row deletion does
      not block subsequent authoring.

## 5. Registration + destructive annotation (only after section 4 passes)

- [x] 5.1 Add `_PRODUCT_SPEC` entry (Tier-2, writes=True) + `DESTRUCTIVE_OPS`
      membership for policy-overwriting ops; regenerate
      `tool_surface_contract.json` via `scripts/dump-tool-schemas.py`.
- [x] 5.2 Update `test_mcp_schema_fidelity.py`/`test_tool_surface_contract.py`/
      `test_connector_guardrails.py` pins; CLI + REST smoke
      (`kb govern_memory list --json`).

## 6. Bootstrap + teaching surface

- [x] 6.1 Red test `tests/test_bootstrap.py::test_bootstrap_reports_governance`;
      add the `governance` payload section + bump `contract_version` in
      `op_bootstrap`.
- [x] 6.2 Add generic `_scaffold/_Schema/references/governance.md` + `SKILL.md`
      section (lifecycle + forged-envelope rule); `tests/test_scaffold_no_leak.py`
      green.

## 7. Gates

- [x] 7.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_govern_memory_tool.py tests/test_bootstrap.py
      tests/test_mcp_schema_fidelity.py tests/test_tool_surface_contract.py
      tests/test_connector_guardrails.py tests/test_scaffold_no_leak.py` green.
- [x] 7.2 Full suite and `uv run python -m pytest tests/test_latency_gate.py -q`
      green.
- [x] 7.3 `uvx ruff check` clean; `openspec validate add-governance-tools
      --strict` green.
