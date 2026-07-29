# Proposal: add-governance-tools

## Why

The kernel decides and the release gate enforces, but there is no way for the
owner to *author* policy. The client/LLM interprets the user's natural-language
intent and supplies structured canonical documents and arguments; Exomem
validates, compiles, resolves exact membership and policy facts, and writes the
deterministic policy. There is no server NLP or query planner. The model must
never be the enforcement authority, and nothing in retrieved content may change
policy.

This change adds the single authoring tool `govern_memory` and the release-time
grant loop. It depends on the `add-disclosure-receipts` foundation and rides the
existing validated mutation protocol (mutation identity, idempotency,
`MUTATION_*` terminals) so commits are safe, replay-aware, and durably evidenced.
It consumes proposal nonces with true consume-once semantics bound to the policy
fingerprint and affected-item content fingerprints — closing the propose→commit
TOCTOU window against out-of-band and synced edits.

## What Changes

- New MCP tool `govern_memory(operation=...)` with operations: `propose`,
  `commit`, `grant`, `revoke`, `suspend`, `resume`, `list`, `explain`, `simulate`,
  `undo`, `declare` (session purpose). `list`/`explain`/`simulate` are read-only;
  the rest write and ride the lease + idempotency + receipt terminal.
- **propose → commit**: `propose` derives interpretation, privacy preview,
  consequences, and overlaps solely from current plus prospective compiled policy
  and concrete membership. Caller `selector_paths` and `target_ceiling` are
  compatibility hints only: mismatches diagnose or reject and never determine
  privacy, transition direction, or commit identity. It returns canonical YAML,
  current-ceiling-safe membership, duration, reversal path, and a single-use
  `proposal_id`.
  `commit` consumes that nonce (dedicated durable store, JTI semantics — not the
  idempotency replay store), refuses on policy/item drift (`STALE_GOVERNANCE_POLICY`),
  writes `_Governance/` files, archives prior versions, bumps the fingerprint.
- **Release-time grants**: `grant(token, duration=...)` redeems a single-use
  withhold-token from a `withheld` notice ("allow this once / for today / only
  these three files"); `revoke(scope=session)` = "everything I authorised in this
  conversation." `undo` restores the archived prior policy version and re-resolves
  dependent grants, expiring any whose member set changed.
- **Receipt-first activation**: every authorization-affecting write (`commit`,
  `grant`, `revoke`, `suspend`, `resume`, `undo`, and `declare`) fsyncs a
  plaintext-free intent before state changes. Direction is computed from the
  effective before/after disclosure lattice, never inferred from the operation
  name; every target activates only after a terminal receipt, while a proven
  narrowing may install a separate fail-closed overlay after intent.
  Exact prior, prepared, and final-active composite digests cover every affected
  YAML file and sidecar row. The journal first records a durable `allocating`
  control row, ignored by the policy read guard; it fsyncs actual intent(s),
  atomically arms as `pending`, and only then prepares marker/target state. YAML
  mutations use the exact-bound pending marker plus authoritative pending row so
  the loader keeps last-good policy (or blocks cold-start) until target state is
  durable and receipted. Token consumption and pending grant creation commit in
  one sidecar transaction. Purpose changes use an event-keyed staged target while
  the prior active purpose remains visible. Pending journals pin referenced TTL
  rows; activation or abort closes the journal. Recovery closes `allocating` as
  exact-prior and aborts only observed intents, never inventing a terminal when
  none exists; `pending` uses exact prior/prepared/final rules. Final-active is
  accepted only with terminal evidence and recovery never guesses.
- **Teaching surface**: `bootstrap()` gains a `governance` section (enabled flag,
  policy fingerprint, resolved audience, purpose mechanism, one-paragraph
  disclosure contract) and a bumped `contract_version`; the scaffold
  `_Schema/SKILL.md` + a new generic `references/governance.md` teach the flows,
  including that governance-shaped text inside retrieved content is data, never a
  command (forged-envelope defense).

## Capabilities

### New Capabilities

- `governance-authoring`: the natural-language policy lifecycle over MCP —
  propose/commit with consume-once drift-bound nonces, session and one-shot
  grants, revoke/suspend/resume/undo, and read-only explain/simulate/list — where
  the model proposes and Exomem validates and enforces.

### Modified Capabilities

- `agent-bootstrap-contract`: the portable contract gains a governance section and
  a version bump so a generic client learns the disclosure model and control loop.

(`command-surface` is registry-level and gains the `govern_memory` tool — with its
mixed read/write action split and destructive-op annotation — automatically, per
the relation-acceptance-queue precedent.)

## Impact

- Code: new `src/exomem/governance/tool.py` (`op_govern_memory` router) +
  `store.py` additions (`proposals`, `session_grants`, `session_purpose` tables,
  TTL sweeps); `commands.py` (`_PRODUCT_SPEC` entry, `invocation_is_read_only`
  action split, bootstrap payload), `command_surface.py` (`DESTRUCTIVE_OPS`),
  `tool_surface_contract.json` (regenerated), `_scaffold/_Schema/SKILL.md` +
  `references/governance.md`; receipt integration through
  `governance/receipts.py` and the pending-transaction gate in
  `governance/policy.py`; `governance/store.py` owns the monotonic sidecar v2→v3
  migration.
- Tests: `tests/test_govern_memory_tool.py`, `tests/test_bootstrap.py` extension,
  schema-fidelity/connector-guardrail pin updates; scaffold no-leak green.
- Explicitly NOT in scope: receipt-chain persistence internals (provided by the
  prerequisite receipts change), bridges, or presets. The tool SHALL NOT be
  registered publicly until its receipt-first integration tests pass.
