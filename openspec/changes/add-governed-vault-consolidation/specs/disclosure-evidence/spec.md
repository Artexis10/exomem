## ADDED Requirements

### Requirement: Consolidation emits versioned plaintext-free lifecycle evidence

Every durable consolidation state transition SHALL emit through the existing
per-machine hash-chained receipt writer. The outer record SHALL remain exactly
the common `receipt/v1` envelope: `schema`, `event_id`,
`event_type=consolidation`, `phase`, `timestamp`, `instance_id`, `seq`, physical
`prev`, `durable`, record `hash`, and one payload member named
`consolidation_event`. The nested member SHALL be a closed object with schema
`exomem.consolidation-event/<kind>/v1`; it SHALL NOT replace, duplicate, or be
mistaken for the outer envelope. Existing sequence/`prev`/`hash` chaining,
critical fsync, tail/anchor recovery, and plaintext prohibition SHALL apply
unchanged.

The nested payload SHALL contain only applicable opaque operation/run
identities, keyed vault/principal/attestation digests,
artifact/manifest/fingerprint digests, policy/plan/action/map/probe digests,
batch ordinal/count, bounded class/outcome counts, timestamps/expiry,
confirmation type, prior/prepared/final state digests, semantic-parent event
id/digest, its role-specific `payload_digest`, and stable content-free outcome
codes. The outer `event_id` SHALL remain the existing critical-receipt identity:
an intent is exactly 64 lowercase hexadecimal characters and its one terminal
is exactly `<intent-event-id>:committed` or `<intent-event-id>:aborted`.
Terminal roles SHALL NOT introduce another 64-hex outer event id; their distinct
identity SHALL be carried by the nested `payload_digest`.

The reconciliation evidence SHALL bind the complete source-to-destination
mapping-set digest, including C1 publication no-ops, without recording paths,
identities, or object refs in plaintext. Retirement evidence SHALL bind the
exact source-archive retention, trusted-custodian transfer, or
external-destruction disposition digest and the retained-versus-irrecoverable
provenance statement digest; post-retirement rollback mode, forward-snapshot
proof where required, and surviving-copy-ledger digest. Clearance issuance,
source-lifecycle consumption, authenticated external completion, and destination
finalization SHALL be different event kinds and terminals. Issuance SHALL not
claim consumption or external disposition; consumption SHALL bind the
single-use JTI/operation, current source fence/checkpoint/census, disposition,
destination verification/recovery/no-loss proofs, and verifier decision; only a
separately verified completion attestation may support finalization.

The event families SHALL cover authenticated intake, immutable snapshot
binding, reconciliation completion, plan materialization with its immutable
control-basis and successor-automaton digests, every trusted render page and
acknowledgement, owner approval/token reservation, seal transitions,
restrictive-policy activation, every content
batch, derived rebuild, each in-process positive/negative probe,
transport-stop/drain, each exact-cell transport probe, transport terminal,
routing-open, abort, rollback planning/render/approval/restore/rebuild/probe,
recovery, and retirement plan/render/approval/clearance/consume/completion/
finalize. The retirement family SHALL separately cover forward-snapshot
verification, each surviving-copy-ledger calculation, pending-forward-only
rollback-fence installation, expiry/non-consumption fence release, and
conversion of that fence in the `retirement-finalize` target to the permanent
disposition. Plan evidence SHALL bind trusted rendering page/coverage and
server-derived impact-summary digests rather than a caller display claim. No
consolidation event schema SHALL
permit an archive or token byte, note/media body, extracted text, path, title,
reference, conflict value, relation/citation content, policy document, raw
principal identifier, human scope label, authentication credential, staging
path, or rollback-preimage byte.

#### Scenario: Joint plan is approved

- **WHEN** the trusted owner confirmation mints a token for an exact plan
- **THEN** evidence records the plan, attestation-set, source/destination snapshot, token-id, expiry, and confirmation-type digests
- **AND** it contains neither token bytes, conflict/path detail, policy text, principal identity, nor content

#### Scenario: Verification finds a negative-probe leak

- **WHEN** a named negative probe returns forbidden content or metadata
- **THEN** evidence records the matrix/probe digest, result class, bounded counts, policy/census fingerprints, and failed terminal
- **AND** expected or observed plaintext and identifying metadata remain only in owner-protected run state

#### Scenario: Retirement clearance is issued

- **WHEN** every retirement prerequisite and its distinct owner confirmation verifies
- **THEN** the receipt records only prerequisite proof digests, source checkpoint digest, destination verification/preimage digests, confirmation type, and clearance outcome
- **AND** it does not claim or log source deletion, key destruction, backup erasure, or account/billing changes

#### Scenario: Source lifecycle consumes retirement clearance

- **WHEN** the source operator consumes the exact unexpired clearance under the current source fence and every checkpoint, disposition, destination, and no-loss digest revalidates
- **THEN** a distinct consume terminal binds the clearance JTI and current proof digests without plaintext
- **AND** neither the earlier issuance nor this consume event is represented as the later external-completion attestation or destination finalization

### Requirement: Every consolidation effect is receipt-first and exactly reconcilable

Before reserving an approval token, changing the destination seal, activating or
restoring policy, committing a content or restore batch, unsealing, aborting,
rolling back, or issuing source-retirement clearance, the coordinator SHALL
append and fsync a deterministic plaintext-free outer `receipt/v1` intent with
its nested consolidation event containing operation identity plus exact prior,
prepared when applicable, and target digests. After classifying the effect it
SHALL append and fsync exactly one suffix-named `committed` or `aborted` outer
terminal for that intent. The corresponding run-journal transition SHALL carry
the outer event id and record hash, nested payload digest, physical receipt head,
and semantic predecessor needed to prove the relationship.

On restart, an unresolved intent SHALL be reconciled only by comparing current
seal, policy, canonical census, artifact, derived, and clearance state with its
recorded exact fingerprints. Exact target SHALL append the missing committed
terminal without replaying the effect; exact prior SHALL append aborted or allow
the recorded safe continuation; a mixed/third state SHALL remain unresolved and
sealed. Identical internal append retries SHALL reuse the deterministic event id
and SHALL not duplicate logical evidence. Missing, divergent, truncated, or
conflicted receipt state SHALL block the affected phase and unseal.

#### Scenario: Crash follows a committed content batch

- **WHEN** canonical files match the batch final fingerprint but the terminal receipt or run-journal acknowledgement is missing after restart
- **THEN** reconciliation appends/adopts the one committed terminal and advances without invoking the batch again
- **AND** the event chain contains one logical intent and one logical terminal for that batch

#### Scenario: Crash follows seal intent but state is mixed

- **WHEN** receipt recovery finds a durable seal intent and current lifecycle state matches neither its exact prior nor target
- **THEN** the intent remains unresolved and the destination fails closed under the seal
- **AND** no ordinary response receives the internal state needed for owner repair

#### Scenario: Receipt append fails before unseal

- **WHEN** all probes pass but the verified or unseal intent/terminal cannot be appended and fsync'd
- **THEN** unseal is refused and the destination remains content-free to ordinary surfaces
- **AND** retry resumes from the same deterministic outer intent event identity

### Requirement: Every durable run effect has explicit cross-store causality

Every durable consolidation effect SHALL use the outer `receipt/v1` envelope
and one nested closed schema named
`exomem.consolidation-event/<kind>/v1`. Every nested role SHALL require exactly
`schema`, `kind`, `run_id`, `operation_id`, `phase`, `record_role`,
`effect_ordinal`, `request_digest`, `prior_digest`, `target_digest`,
`semantic_parent_event_id`, `semantic_parent_payload_digest`, and
`payload_digest`; it SHALL require exactly the applicable one of
`batch_ordinal`, `rebuild_ordinal`, `probe_ordinal`, or `page_ordinal`, require
`prepared_digest` only for a kind with a distinct prepared state, and forbid
all inapplicable ordinals and fields. `effect_ordinal` and every specialized
ordinal SHALL be an integer in `0..2^31-1`. An `intent` SHALL forbid
`observed_digest`; `committed|aborted` SHALL require it. All digests SHALL be 64
lowercase hex and all ids SHALL follow the outer-id grammar below. Nested
`record_role` SHALL equal outer `phase`; every consolidation record SHALL set
outer `durable=true`, and any mismatch SHALL fail validation/recovery.

Exactly when an intent's committed target would create a successor context,
that intent and its matching committed or aborted terminal SHALL additionally
require `successor_context_seed_digest`; all other nested payloads SHALL forbid
it. It SHALL equal the digest of the closed predecessor-free seed in the shared
plan-entry contract. The intent `target_digest` preimage and committed
`observed_digest` preimage SHALL include that same seed digest, never the full
context digest/ref or terminal id/payload digest. The terminal's own
`payload_digest` therefore commits the seed without depending on the full
context derived after it.

The nested `payload_digest` SHALL be SHA-256 over the common RFC 8785/JCS and
length-framing contract with ASCII domain
`exomem.consolidation-event-payload/<kind>/<record_role>/v1` and the closed
nested object excluding `payload_digest`. The deterministic outer intent
`event_id` SHALL be SHA-256 with domain
`exomem.consolidation-event-id/<kind>/v1` over the closed intent identity:
`schema`, `kind`, `run_id`, `operation_id`, `phase`, `record_role=intent`, all
applicable ordinals, `request_digest`, `prior_digest`, optional
`prepared_digest`, `target_digest`, `semantic_parent_event_id`, and
`semantic_parent_payload_digest`. The physical receipt `prev`, outer sequence,
timestamp, record hash, and nested `payload_digest` SHALL NOT enter this
identity. The terminal outer id SHALL be exactly
`<intent-event-id>:committed|aborted`, while its nested role-specific digest
binds `observed_digest`. Thus an identical retry adopts one intent/terminal;
an intentional later identical action has a new explicit operation id.

| Plan kind | Exact allowed current committed semantic terminal kind(s) | Exact product logical terminal(s) that durably return the `plan-materialize` context pair |
|---|---|---|
| `cutover` | `reconcile` with `unresolved=0` and cutover eligibility; `repair-terminal` that commits/adopts such a `reconcile` | `reconcile(phase=reconcile)`; `recover(phase=repair-terminal)` only for that repair; `status(detail=owner-detail)` observing either allowed current terminal |
| `rollback` | `complete`; `rollback-complete`; `retirement-pending-forward-only`; `retirement-finalize`; `repair-terminal` that commits/adopts one of those four kinds | at `complete`: `apply(phase=complete)` or `verify(verification_kind=transport,phase=complete)`; at `rollback-complete`: `rollback(rollback_mode=nonterminal-contingency,phase=rollback-complete)` or `rollback(rollback_mode=terminal-plan,phase=rollback-complete)`; at `retirement-pending-forward-only`: `status(detail=owner-detail)` only; at `retirement-finalize`: `retire-source(phase=finalize)` whose terminal phase is `retirement-finalize`; at an allowed repair: `recover(phase=repair-terminal)`; at every allowed current terminal: `status(detail=owner-detail)` |
| `retirement` | real-cutover `complete`; `repair-terminal` that commits/adopts real-cutover `complete` | `apply(phase=complete)` or `verify(verification_kind=transport,phase=complete)` only when the current run is real-cutover and retirement prerequisites are eligible; `recover(phase=repair-terminal)` only for that repair and eligibility; `status(detail=owner-detail)` observing either allowed current terminal and eligibility |

This table is closed. Every effect whose committed target creates a successor
context SHALL first create or adopt, inside the already-required owner-only
idempotency reservation, one inert future-context reservation containing an
opaque future ref, canonical seed bytes, and seed digest `S`. The closed seed
JCS object has schema `exomem.consolidation-successor-context-seed/v1` and
exactly `schema`, `context_schema`, `context_kind`, `run_id`, `run_revision`,
`destination_binding_digest`, `owner_binding_digest`, `basis_digest`,
`successor_action`, `successor_variant`, `issued_at`, `expires_at`, `nonce`, and
`facts`. `schema` equals the seed schema, `context_schema` equals
`exomem.consolidation-successor-context/v1`, and every other value equals the
eventual full context value. The seed MUST NOT contain
`context_seed_digest`, `predecessor_event_id`, `predecessor_payload_digest`, a
full-context digest/ref, or any receipt id/hash/head. `S` is SHA-256 over the
common length-prefixed frame with exact ASCII domain
`exomem.consolidation-successor-context-seed/v1` and the seed JCS bytes.

The inert reservation is fsynced before the intent, changes no semantic state
or run revision, and cannot be resolved or returned. It is keyed by the
existing vault/installation-generation/owner/operation reservation and also
binds action, canonical request digest, run, effect kind, and effect ordinal;
only that byte-identical operation may adopt it. The successor-producing
intent's target-digest preimage and nested payload contain
`successor_context_seed_digest=S`; its prepared journal and matching terminal
carry the same `S`. That field is required for an intent whose target would
create a context and its committed or aborted terminal, and forbidden for every
other effect. Neither receipt role, `target_digest`, nor `observed_digest`
contains the full context digest/ref or the not-yet-existing terminal
id/payload digest.

After the committed terminal is fsynced, let `P` be its already-final nested
payload digest. The coordinator deterministically derives the full context by
copying every non-schema seed field, setting `schema=context_schema`, adding
`context_seed_digest=S`, `predecessor_event_id` equal to that committed outer
terminal id, and `predecessor_payload_digest=P`, then computes
`successor_context_digest` under the full-context domain. The run-journal final
SHALL fsync the seed bytes/digest, opaque ref, full context bytes/digest, and
terminal outer id/payload digest/hash/sequence/new physical head together. That
final is the sole point at which the context becomes current or resolvable; the
derivation emits no second semantic event and does not advance run revision.
Only after that final may the idempotency record fsync canonical logical
terminal `T`, whose trusted output binds the opaque ref and full-context digest,
and only then may a product terminal return it. Receipt intent/terminal bind
`S`; journal final binds `S`, `P`, and the full-context digest; product output
binds the ref/full-context digest and does not expose `S`. The dependency graph
is therefore `S -> P -> full context`, never `full context -> P`.

Recovery treats a seed reservation without an intent as inert; an identical
retry adopts it and changed identity/request/effect data conflicts. The existing
intent/prepared/effect/classification recovery rules then apply. A committed
terminal without final deterministically reconstructs only the same full
context from its reserved seed plus terminal id/`P` and writes final without a
new event, effect, or revision. A final without the idempotency logical terminal
writes only byte-identical `T`; retry then reports replayed delivery. An aborted
terminal or nonrow effect never materializes a context. Missing, divergent, or
cross-bound seed bytes/digest, ref, terminal, full-context digest, journal, or
idempotency data fails closed before return or successor admission.

For an allowed terminal, one context may list both `rollback` and `retirement`
at eligible real-cutover `complete`. At the internal
`retirement-pending-forward-only` checkpoint, the pair is returnable only after
the same durable derivation and only by owner-detail status; continuing
retirement emits a successor and stales it. A product terminal not listed for
the exact row, phase, mode, target repair, run mode, or eligibility SHALL return
no plan-materialize pair and cannot be a plan intent parent. In this table,
`repair-terminal` that commits/adopts kind K means its preceding
`recover-classification` references K's unresolved intent/terminal, exact
classification observes K's target digest, and the committed repair payload
binds that reference and observed digest; no caller field selects K.

The immediate semantic parent for every intent SHALL follow this closed table;
the parent of each terminal SHALL instead be its matching intent event and
intent payload digest:

| Kind | Exact intent semantic parent |
|---|---|
| `start` | fixed consolidation semantic root defined below |
| `intake` | committed `start` |
| `snapshot-source` | committed `intake` |
| `snapshot-destination` | committed `snapshot-source` |
| `reconcile` | committed `snapshot-destination` |
| `plan-cutover` | exact current committed terminal allowed by the `cutover` row of the shared plan-entry table; its event id/payload digest equal the resolved successor-context predecessor |
| `plan-rollback` | exact current committed terminal allowed by the `rollback` row of the shared plan-entry table; its event id/payload digest equal the resolved successor-context predecessor |
| `plan-retirement` | exact current committed terminal allowed by the `retirement` row of the shared plan-entry table; its event id/payload digest equal the resolved successor-context predecessor |
| `render-begin` | committed matching `plan-cutover`, `plan-rollback`, or `plan-retirement` |
| `render-page` ordinal 0, then ordinal i greater than 0 | committed `render-begin`, then committed `render-ack` ordinal i-1 |
| `render-ack` ordinal i | committed matching `render-page` ordinal i |
| `render-complete` | committed last `render-ack`; every stored plan has at least one page |
| `approval` | committed matching `render-complete` |
| `token-reservation` | committed matching `approval` |
| `seal-intent`, `seal-drained`, `preimage`, `policy-prepare`, `policy-active` | respectively committed cutover `token-reservation`, `seal-intent`, `seal-drained`, `preimage`, `policy-prepare` |
| `content-batch` ordinal 0, then ordinal i greater than 0 | committed `policy-active`, then committed `content-batch` ordinal i-1 |
| `rebuild-kind` ordinal 0, then ordinal i greater than 0 | committed last `content-batch`, then committed `rebuild-kind` ordinal i-1 |
| `in-process-probe` ordinal 0, then ordinal i greater than 0 | committed last `rebuild-kind`, then committed `in-process-probe` ordinal i-1 |
| `in-process-verified` | committed last mandatory `in-process-probe` |
| `transport-stop`, `transport-probe` ordinal 0, later `transport-probe`, `transport-verified`, `routing-open`, `complete` | respectively committed `in-process-verified`, `transport-stop`, prior probe, last probe, `transport-verified`, `routing-open` |
| `abort-begin` | exact committed current apply phase in the pre-publication closed set cutover `token-reservation`, `seal-intent`, `seal-drained`, `preimage`, `policy-prepare`, `policy-active` |
| `abort-policy-restore`, `abort-candidate-cleanup`, each `abort-rebuild-kind`, each `abort-probe`, `abort-complete` | committed latest applicable earlier abort event in this fixed order, beginning with `abort-begin`; policy restore/rebuild exist exactly when the recorded prior/target state requires them |
| `rollback-nonterminal-contingency-begin` | exact committed current nonterminal apply phase named by the sealed apply journal |
| `rollback-terminal-plan-begin` | committed rollback-plan `token-reservation` |
| `rollback-seal`, `rollback-revalidate`, each `rollback-restore-batch`, each `rollback-rebuild-kind`, each `rollback-probe`, `rollback-complete` | committed branch begin, then committed immediately preceding rollback phase/batch/kind/probe in the approved order |
| `recover-classification` | exact unresolved intent or terminal being classified |
| `repair-terminal` | committed matching `recover-classification` |
| `forward-snapshot-verified` | committed retirement-plan `token-reservation` |
| each `surviving-copy-ledger` | committed immediately preceding retirement effect, starting with token reservation or `forward-snapshot-verified` according to approved mode |
| `retirement-pending-forward-only` | committed forward-only surviving-copy ledger |
| `retirement-clearance` | committed current surviving-copy ledger, with committed pending-forward-only between them exactly in forward-only mode |
| `retirement-pending-fence-release` | committed expiry/non-consumption/source-survival `recover-classification` for that unconsumed clearance |
| source-chain `retirement-consume` | authenticated destination `retirement-clearance` terminal |
| destination `retirement-completion` | authenticated source `retirement-consume` terminal |
| `retirement-finalize` | committed destination `retirement-completion` |

The fixed start root id and payload digest SHALL both equal SHA-256 of the common
frame for ASCII domain `exomem.consolidation-semantic-root/v1` and an empty JCS
object. Only `start` MAY name that root. No request body MAY supply or override a
semantic parent. Every individual batch, rebuild kind, probe, rendered page,
acknowledgement, and phase SHALL have its own event; aggregation SHALL not erase
its ordinal.

The outer `prev` SHALL always equal the actual current local outer receipt
record hash at append time. It is physical chain order only; it MAY name an
unrelated event or run and SHALL never be represented as semantic causation.
`semantic_parent_event_id` and `semantic_parent_payload_digest` SHALL encode the
table's logical ancestry independently. A concurrent unrelated append may
advance physical `prev` without changing the deterministic intent id or staling
a plan; a missing, wrong, replayed, or reordered semantic predecessor SHALL
fail closed even when the outer receipt chain is otherwise valid.

`retirement-pending-forward-only` SHALL commit before a forward-only clearance
event and SHALL name the exact forward-snapshot and surviving-copy-ledger events.
Its release event SHALL be authorized only by the exact expired, unconsumed JTI
and unchanged-source/archive survival proof; finalization instead records its
permanent conversion.

`retirement-consume` SHALL be emitted on the source operator's per-machine chain
with the authenticated destination clearance event id as external semantic
parent. `source-retirement-completion/v1` SHALL bind that consume terminal id,
event digest, and verified source receipt-head digest. The destination
`retirement-completion` event SHALL use the authenticated consume terminal as
semantic parent, while its ordinary `prev` remains the local destination receipt
head; `retirement-finalize` then names the destination completion event. This is
cross-chain causation, not cross-machine atomicity, and a missing/divergent source
terminal or head blocks finalization.

#### Scenario: Every plan intent parent comes from the shared table

- **WHEN** a cutover, rollback, or retirement plan intent is attempted from every allowed current terminal and every nonrow terminal/repair target
- **THEN** each allowed case uses the returned context's predecessor event id/payload digest as its exact semantic parent, including rollback-complete, retirement-pending-forward-only, retirement-finalize, and their allowed repairs
- **AND** every nonrow produces neither a plan-entry context nor a plan intent, even when its physical receipt chain is valid

JSONL receipt/anchor, SQLite idempotency/registry state, run journal, canonical
run state, policy journal, private artifact store, and canonical filesystem
SHALL be treated as separate stores with no cross-store atomicity claim. A
successor-producing effect first fsyncs only the inert seed/ref subrecord inside
its existing idempotency reservation; this is not a semantic effect or revision
and is not resolvable. Every effect then SHALL use exactly this order:

1. append/fsync the outer intent plus nested payload and observe its physical
   sequence, record hash, and current local head;
2. persist/fsync run-journal `prepared` referencing the outer intent id, nested
   payload digest, outer record hash/sequence/head, semantic parent, and exact
   request/prior/prepared/target digests;
3. perform the one effect or durable run-state transition;
4. classify current state exactly as prior, prepared, target, or mixed/third;
5. append/fsync exactly one suffix-named `committed` or `aborted` outer terminal
   whose nested payload names the intent as semantic parent and binds the
   observed state; and
6. persist/fsync run-journal `final` referencing terminal outer id, nested
   payload digest, outer record hash/sequence, and new physical receipt head.
   For a committed successor-producing effect, this same final SHALL also hold
   the seed bytes/digest, opaque ref, and full context bytes/digest derived from
   that terminal; an aborted or non-producing effect SHALL hold none of them.

Only after step 6 MAY an action-level idempotency terminal be fsynced with the
canonical logical result and any returned full-context ref/digest; response
delivery follows that fsync. An internal effect that is not the product action's
terminal omits this action-level write. No receipt, target/observed digest, or
prepared journal may bind the full context, because its predecessor terminal
does not yet exist.

Recovery SHALL classify every gap: no intent means no effect was admitted;
intent without prepared journal may close aborted only on exact prior and SHALL
block on target/mixed; prepared plus exact prior may resume only the already
authorized effect or abort according to its phase; exact target appends/adopts
the missing terminal; terminal without final journal writes only final,
including its conditional deterministic context derivation; prepared
or final journal referencing a missing/wrong/divergent receipt/head is tamper/
repair-blocked; mixed/third state stays sealed. For a successor-producing
effect, a seed reservation without intent remains inert; terminal without final
reconstructs the full context deterministically from the exact seed plus that
terminal and writes only final; final without the action-level idempotency
terminal writes only the byte-identical logical result. A seed/full-context/
terminal mismatch is repair-blocked. Receipt-file-ahead anchor repair SHALL
verify the chain before adoption. Recovery SHALL never assume a SQLite,
JSONL/anchor, run-file, artifact, policy-journal, or filesystem write committed
because another store says so and
SHALL never replay semantic reasoning.

#### Scenario: Crash occurs at every cross-store edge

- **WHEN** a crash occurs after each ordered step for start, intake, snapshot, reconcile, plan, rendering, approval, seal, policy, a batch, rebuild, each probe kind, transport verification, abort, rollback, or retirement, plus seed-reservation and final-before-idempotency gaps for every successor producer
- **THEN** restart uses exact store/state evidence to append/adopt at most one terminal and never repeats the semantic effect
- **AND** terminal-before-final reconstructs the same full context without a receipt or revision while missing/divergent seed causality keeps the destination sealed or the pre-publication run blocked

#### Scenario: Run journal final exists without its terminal

- **WHEN** run state claims final but the referenced terminal/head is absent or divergent
- **THEN** recovery treats it as cross-store inconsistency rather than success
- **AND** no later effect, unseal, routing-open, rollback, or retirement consumes that claim

#### Scenario: Unrelated receipt interleaves before a consolidation intent

- **WHEN** another valid receipt advances the local physical chain after the declared semantic predecessor but before the next consolidation intent append
- **THEN** the new outer record uses that actual receipt hash as `prev` while its nested semantic parent remains the declared consolidation predecessor
- **AND** deterministic intent identity and plan ancestry remain valid without claiming the two parents are equal

#### Scenario: Terminal keeps the existing outer id grammar

- **WHEN** one consolidation intent with a 64-hex outer id reaches exact target
- **THEN** its durable terminal outer id is exactly `<intent-id>:committed` and its role-specific nested payload digest binds the observed target
- **AND** a second 64-hex terminal id, competing suffix terminal, or terminal with another semantic parent is rejected

### Requirement: Consolidation control and evidence state cannot become policy or knowledge

`Knowledge Base/_Consolidation/**`, private artifact storage, consolidation seal
markers, and consolidation journals SHALL be excluded from knowledge indexing,
policy source discovery, policy fingerprints/signatures, policy-cache
dependencies, prospective-policy input, unknown-policy-file findings, and
generic conflict-copy handling. The existing `_Governance/events/**` evidence
namespace SHALL remain append-only evidence rather than policy input.

Receipt and run-state churn SHALL not alter the canonical destination snapshot
fingerprint used by an active plan. The entire `_Consolidation/**` subtree, all
seal/journal/control state, and every receipt chain for current, older, and
concurrent runs SHALL be excluded structurally from content census, preimage,
and rollback target. The plan SHALL bind its immutable
`control_basis_digest`/successor-automaton digest, while each later request
resolves its owner-only successor context and each journal transition separately
binds the protected exact current semantic predecessor and
the outer physical receipt facts observed for that append; it SHALL NOT compare
the current mutable global head or complete run-control tree with the old plan
preimage. No restore SHALL overwrite any excluded subtree. A conflict, malformed record, missing chain
suffix, or tamper finding in consolidation evidence SHALL fail new critical
append and the affected recovery/unseal path closed while leaving the last
verified active policy enforceable. Detailed inventories and conflicts SHALL
remain owner-only run data; released summaries MAY carry only bounded counts and
opaque run/digest references.

#### Scenario: Consolidation receipts append during policy activation

- **WHEN** critical consolidation events are appended before and after an exact policy transition
- **THEN** the prospective/active policy fingerprint and approved destination corpus fingerprint do not churn because of those events
- **AND** cold policy load does not compile the receipts or run state as policy

#### Scenario: Evidence conflict appears during recovery

- **WHEN** synchronization creates a conflicted or divergent copy in receipt or consolidation control state
- **THEN** existing policy continues to enforce while new consolidation effects and unseal fail closed
- **AND** audit reports content-free evidence/control-state repair facts to the owner

### Requirement: Abort and rollback preserve attempted-cutover evidence monotonically

Abort, rollback, software downgrade, preimage restoration, and source-retirement
handling SHALL NOT delete, truncate, overwrite, or restore backward any
governance, consolidation, mutation, disclosure, or deletion receipt chain.
Receipt files and anchors SHALL not be restored from the destination preimage.
Abort and rollback SHALL append their own intent, phase, causation, exact-state,
and terminal evidence linked to the original plan/apply operation so a failed or
reversed cutover remains auditable without retaining plaintext.

#### Scenario: Preimage predates cutover receipts

- **WHEN** rollback restores canonical destination state from a preimage created before policy/content publication
- **THEN** every later cutover receipt remains present and chain-valid
- **AND** new rollback receipts link the restored-state proof to the original operation without restoring an earlier receipt head

#### Scenario: Software release is rolled back with a nonterminal run

- **WHEN** an older compatible recovery binary opens the destination
- **THEN** it preserves unknown/newer receipt records and schema versions monotonically
- **AND** it cannot erase consolidation evidence merely to regain readiness
