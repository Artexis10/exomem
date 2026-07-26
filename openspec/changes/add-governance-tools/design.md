# Design: add-governance-tools

## Context

Reuses verified machinery. Mutation safety: `writer_lease.invoke_command`
(lease + idempotency), `mutation_terminal.committed_terminal` (`:82`, receipt
envelope). Consume-once nonces: `hosted_transfer.consume_transfer_jti`
(`:333-353`) — the correct primitive, distinct from the idempotency *replay*
store (`writer_lease.IdempotencyStore`), which returns a cached result on
key reuse rather than refusing a spent nonce. Fingerprint-guarded commit:
`relation_registry.save_registry` (`:230-252`, `STALE_*` on `expected_hash`
mismatch). Mixed read/write dispatch: `commands.invocation_is_read_only`
(`:5442`, per-command action allowlists). Bootstrap payload:
`op_bootstrap:211` (versioned, code-authored, `content_included: False`).

## Goals / Non-Goals

Goals: one natural-language authoring tool; safe propose→commit that cannot be
replayed or applied against drifted state; zero-friction release-time grants;
revoke/undo that keep history and dependent grants coherent; a teaching surface
so any client uses the operations correctly.

Non-Goals: receipt-chain persistence internals (provided by the prerequisite
`add-disclosure-receipts` change), bridges, presets, and a policy UI. No model
call enters the enforcement or validation path — the LLM proposes, Exomem
decides.

## Decisions

### D1 — One tool, operation-routed
`govern_memory(operation=...)` matches Exomem's multi-operation tool style and
respects client tool-count limits. `list`/`explain`/`simulate` classify read-only
via a new `_GOVERN_MEMORY_READ_ONLY_ACTIONS` set in
`invocation_is_read_only`; `propose`/`commit`/`grant`/`revoke`/`suspend`/
`resume`/`undo`/`declare` write and ride the lease + idempotency boundary.
Policy-overwriting operations join `DESTRUCTIVE_OPS` so annotations warn.

### D2 — propose stores a drift-bound nonce; commit consumes it
`propose` writes a pending record to `.governance.sqlite proposals`
(`id, created_at, expires_at, proposal_json, fingerprint_at_propose, affected_item_fingerprints, status`)
with a TTL. `commit(proposal_id)` reserves it for the deterministic operation id
under `BEGIN IMMEDIATE` (JTI semantics — NOT the idempotency replay store), and
only that event may retry. If authoritative policy remains exact prior,
reconciliation releases the reservation and aborts. The reservation lives in
the operation journal as control metadata, so the proposal row remains logically
available while prepared; after exact prepared and terminal evidence, D7 removes
the marker and the one activation transaction marks the proposal spent alongside
all other final sidecar rows and closes the operation journal.
The nonce is therefore consumed exactly once on success but not lost to a crash
before the policy mutation. Commit refuses with `STALE_GOVERNANCE_POLICY` if the
current policy fingerprint or any affected-item content fingerprint differs from
propose time. This closes the TOCTOU window: an out-of-band edit or a
`file_watcher` reindex between propose and commit forces a re-propose rather than
applying a decision computed against vanished state.

### D3 — propose previews respect current ceilings
The resolved-membership preview renders counts and samples at each member's
*current* effective ceiling — a proposal for a scope that would restrict N pages
must not leak those N titles to the model before the rule exists. `explain` and
`simulate` toward a sub-ceiling audience return counts + rule-ids only, never
member titles/excerpts.

### D4 — Grants: session in sqlite, standing in YAML
Release-time `grant` redeems a withhold-token (from `add-release-gate`) and writes
an ephemeral session grant to `.governance.sqlite session_grants` (TTL'd, never a
YAML file) so the lattice sees it after receipt-backed activation; a durable
standing exception is a `grants/*.yaml` file. `revoke(scope=session)` clears all
session grants for the session; `declare` sets a session purpose default. The
model can only redeem tokens Exomem minted — it cannot enlarge scope it was not
offered.

Token redemption and grant creation remain distinct receipt events with one
causation id, but not separate state commits. The writer fsyncs both intents,
then one SQLite `BEGIN IMMEDIATE` validates/consumes the token and creates the
exact pending grant row atomically. Only after both idempotent committed receipts
exist does it activate the grant. A crash before the SQLite commit leaves exact
prior and aborts any begun child; a crash after it leaves the exact prepared
composite, and reconciliation finishes missing child terminals without
re-redeeming the token. Thus approval cannot be consumed without either its
recoverable pending grant or an exact-prior abort.

### D5 — undo re-resolves dependents
`undo` restores the archived prior policy version and re-resolves grants that
depended on the restored version's selectors, expiring/flagging any whose member
set changed — so a restore never silently widens or narrows a grant's reach
against a version it was never reviewed for. The undo transition's composite
digest includes the target YAML plus every dependent-grant row; policy cannot
activate while a stale grant remains outside that target.

### D6 — Teaching surface, forged-envelope defense
`bootstrap` gains a `governance` section and a bumped `contract_version`; scaffold
`SKILL.md` + generic `references/governance.md` teach the lifecycle and state that
governance notices/grant-hints travel only in reserved top-level response keys and
that governance-shaped text inside `excerpt`/`body`/`content` is data, never a
command. Kept generic — `tests/test_scaffold_no_leak.py` enforces no personal
tokens.

### D7 — Receipt-first, composite-state, activation-last
Every authorization-affecting write has a deterministic critical event id and
reuses `governance.receipts.begin_event`/terminal append from the prerequisite
receipt foundation. The invariant is stronger than "write a receipt after the
mutation":

1. compute phase-domain-separated canonical prior, prepared, and final-active
   composite digests over every affected YAML path (content hash or absence) and
   authorization-state sidecar row (table, primary key, row hash, status),
   including proposal consumption and dependent-grant changes;
2. append and fsync a plaintext-free `intent` with operation, all three composite
   digests, and content-free affected ids;
3. create an authoritative pending operation row, then prepare the whole target
   under the existing writer lease/mutation guard;
4. append and fsync `committed` while the target is still pending;
5. activate last, then return a committed mutation terminal.

The engine compares the resolved effective release lattice before/after across
the affected membership, audiences, purposes, and levels. A transition is pure
narrowing only if the target is pointwise no more permissive everywhere; any
incomplete proof is widening/unknown. This is never inferred from operation
name: `suspend`, `resume`, `undo`, `commit`, and `declare` can each go either
direction. Every target still activates only after terminal evidence. After
intent, a proven narrowing MAY install a separate fail-closed overlay; a
widening/unknown pending transition retains prior enforcement (warm) or BLOCKED
(cold) and never exposes target state early.

YAML policy operations (`commit`, `suspend`, `resume`, `undo`, and standing
grant/revoke) create a reserved `_Governance/.policy-mutation.pending.json`
marker before replacing active documents. The marker is content-free and names
the event id, operation, three composite digests, and affected relative
paths/sidecar-row ids. The operation row is the authoritative activation guard;
the marker and operation journal are control metadata excluded from logical
composite inputs because the journal stores those digests and would otherwise be
self-referential. Reconcile validates them separately by event id, embedded
digest set, affected-id set, and `pending`/`closed` phase. `policy.load` checks
any pending operation row before the marker and before returning cache: a
warm process keeps the last-good policy plus
any proven-narrowing overlay and a blocking finding; cold start returns the
existing BLOCKED L0 floor. Direct manual YAML edits remain unchanged when
neither guard exists.

Session grants, purpose state, proposal consumption, and dependent-grant changes
have explicit prepared and final statuses keyed by the same event id. The
prepared composite hashes the prepared row encodings; final-active hashes their
post-activation encodings. The evaluator ignores every prepared target row;
only the separate proven-narrowing overlay may reduce disclosure earlier.
Proposal reservation is journal control metadata and leaves the proposal row
logically available until the activation transaction marks it spent.

State classification is the tuple of journal phase, required terminal set, and
the phase-domain-separated logical component digest. With a `pending` journal,
components matching prior are exact prior and components matching prepared are
exact prepared; anything between them is partial. The final-active digest is the
prospective after-image of the activation transaction and is accepted only when
all terminals exist. Prepared and final-active remain distinct for YAML-only
operations even when their YAML bytes are identical because their digest domains
differ and the pending journal still blocks prepared policy.

Every TTL/GC path consults pending journals and pins each referenced token,
proposal, grant, purpose, or dependent row, including consumed/pending rows,
until exact-prior abort or activation. Logical expiry may make a pending grant
non-authorizing, but physical deletion cannot destroy its recovery composite.

After terminal evidence, activation removes and directory-fsyncs the filesystem
marker while the pending operation row still blocks target policy. One SQLite
transaction then changes every affected sidecar row from its prepared to final
encoding, verifies the final-active after-image and terminal set, and marks the
operation journal `closed`. Thus a crash between marker removal and the
transaction still matches the same logical prepared composite and remains
blocked; a session-only transition activates wholly inside that transaction.
Closed/aborted journals retain their receipt ids for bounded diagnostics but are
retired from live composite reconciliation, so later legitimate TTL deletion or
state evolution cannot invalidate historical final state.

Before any later governance authoring call, reconcile validates the operation
row/marker and hashes every component named by the three composites. Exact prior
appends or recognizes `aborted`, clears guards/reservations, and closes the
journal. Exact prepared appends or recognizes every required committed terminal,
then runs only the activation-and-close transition above. Closed journals are not
rechecked against live component rows. Any mixed, partial, or other open state
remains BLOCKED for manual repair. Reconciliation never repeats the user's
semantic mutation. The compound grant case above is not replay: token consumption
and pending-grant creation were one atomic sidecar transition, and reconcile only
materializes missing evidence and activation.

Registration is last: `_PRODUCT_SPEC`, generated schemas, CLI, and REST exposure
are not changed until tests prove every authorization-affecting operation has a
receipt mapping and the crash points above recover correctly. `propose` stores a
nonce but changes no authorization state, so it is explicitly outside this
mapping; `declare` is inside because purpose changes the release decision.

## Risks / Trade-offs

- **Byte-pinned tool surface**: the new tool + params change
  `tool_surface_contract.json` and connector-guardrail pins; regenerate via
  `scripts/dump-tool-schemas.py` in-task.
- **Tier placement**: `govern_memory` is Tier-2 (policy administration is
  desk-side; hosted cells with `EXOMEM_DISABLE_TIER2` drop the admin tool) — but
  release enforcement still runs everywhere, so decisions hold even where the
  admin tool is absent. The spec states this explicitly.
- **Injected commit attempts**: content cannot mint a valid `proposal_id`; a
  forged id fails the consume-once + fingerprint check. A model *claiming* user
  consent from injected text remains a documented client-side residual, bounded by
  the propose→confirm gate, token narrowness, and revoke-session.
- **Manual edits during a tool transaction**: the reserved pending marker makes
  the loader retain last-good/BLOCKED rather than compiling a hybrid. A current
  fingerprint matching neither prior nor target is never auto-resolved.

## Migration Plan

Additive tool + bootstrap version bump (existing contract discipline). No
existing policy migration; the receipts foundation leaves sidecar schema v2 and
`governance/store.py` alone owns the monotonic v2→v3 tools migration. Every
opener preserves newer versions. Absent
`_Governance/`, `list`/`explain` report "governance: disabled" and no file is
written until a `commit`.

## Open Questions

None blocking. Preset shortcuts (`propose(preset="legal")`) are deferred to
`add-professional-presets`.
