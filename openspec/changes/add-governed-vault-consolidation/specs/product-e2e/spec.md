## ADDED Requirements

### Requirement: Installed-wheel E2E proves the complete managed-vault consolidation loop

The product gate SHALL build and install the wheel into an isolated environment,
initialize two distinct governed temporary vaults with Sources, Evidence,
compiled notes, Records, media/sidecars, semantic units, durable identities,
history/supersession, relations, citations, review state, and different policy
principals; adopt/bind them as distinct local logical vault/installations using
private cell/machine-owned trust; and create a quiesced authenticated
content-addressed source export through the installed product. Through a real
owner-authorized client it SHALL complete start,
inventory, all C1-C8 reconciliation classes, fresh destination-principal
attestation, exact joint plan, trusted render of every server-defined plan
section/page and impact summary, owner approval, seal, restrictive-policy
activation, multi-batch publication, derived rebuild, positive/negative
in-process verification, stopped/drained-routing exact-cell black-box transport
verification, routing open, restart, and persistent re-verification.

The test SHALL prove the source archive and source canonical census remain
byte-identical, the destination equals the approved plan, every preservation
class and relationship resolves, no source audience/grant/token/session became
destination authority, and rebuilt derivatives refer only to canonical
destination bytes. It SHALL run without an optional server-side reasoning model
or network dependency and SHALL use finite structured conflict decisions.

#### Scenario: Installed consolidation completes

- **WHEN** the installed-wheel fixture supplies valid authenticated snapshots, resolves every conflict, and confirms the exact joint plan
- **THEN** the destination completes and remains correct after process restart with all canonical content, lineage, review, policy, and derived invariants proven
- **AND** the source bytes/checkpoint remain unchanged and no source authority identifier is active in the destination

#### Scenario: Intake or review is unsafe

- **WHEN** the fixture varies an archive byte, signature/receipt claim, snapshot, principal attestation, conflict decision, policy byte, plan digest, token JTI, expiry, or batch partition
- **THEN** the installed product fails at the earliest applicable gate without unreviewed publication
- **AND** the source remains unchanged and the destination is either exact prior state or durably sealed for recovery

#### Scenario: Local identity binding is copied or caller-selected

- **WHEN** the installed fixture copies an identity record to a second root, reuses an installation id, omits the machine-owned trust key, or submits replacement ids/verifier keys in the request
- **THEN** export/start refuses before inventory and no cell is accepted as an authenticated source or destination
- **AND** an explicit rehearsal clone succeeds only with new active ids and immutable clone-of lineage

#### Scenario: Adopted source changes normally before export

- **WHEN** the fixture adopts a legacy source, performs an ordinary legitimate canonical write, then quiesces and exports it
- **THEN** the stable identity/root binding remains valid while the Ed25519 attestation binds the new current census
- **AND** equality with the immutable adoption census is not required

#### Scenario: Cell id and vault id are equal

- **WHEN** a Hosted binding or owner-entitlement fixture aliases routing `cell_id` to logical `vault_id`
- **THEN** installed startup/admission refuses before owner-context construction or run lookup
- **AND** the distinct installation id/generation does not make that alias valid

#### Scenario: Failover attempts split-brain activation

- **WHEN** a logical-id-preserving restore tries to activate generation N+1 without atomically fencing source generation N, or both generations advertise readiness
- **THEN** the target remains offline and stale-source admission is refused once fencing commits
- **AND** only the exact fresh-installation transfer operation can recover source-active, target-pending, or target-active state

#### Scenario: Non-owner invokes each action

- **WHEN** every consolidation action is attempted with valid-looking identifiers under a non-owner or unresolved principal
- **THEN** each fails before existence lookup, parsing, allocation, or mutation with equivalent content-free output
- **AND** no non-owner can initiate import work or learn whether a run, artifact, source, or destination exists

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

#### Scenario: Plan-entry reachability and causality are table-exhaustive

- **WHEN** installed E2E visits every allowed current terminal and listed product terminal in each plan-kind row, including both rollback modes, the pending-forward status checkpoint, finalize, and every allowed repair target
- **THEN** the returned pair is durable, its eligible-kind set contains exactly the applicable rows, and materialization emits a matching plan intent whose semantic parent equals the context predecessor
- **AND** every unlisted action/phase/mode/repair target/run mode/eligibility/terminal combination returns no pair, omits `plan`, and refuses materialization without an intent

#### Scenario: Plan advances through only its declared successors

- **WHEN** the fixture passes each owner-only successor-context pair from reconciliation through materialization, every ordered render/page acknowledgement, approval, and apply token reservation
- **THEN** apply accepts the immutable control basis as an ancestor and separately binds the token-reservation predecessor to `seal-intent` even when an unrelated receipt advanced physical `prev`
- **AND** a skipped, reordered, duplicated, replayed, or unexpected same-run event makes the plan stale before publication

#### Scenario: Lost protected output remains reachable through owner status

- **WHEN** an eligible reconciliation, plan/render, approval, terminal cutover planning-eligibility, or sealed nonterminal-contingency terminal is committed but its response is lost
- **THEN** authenticated `status(detail=owner-detail)` returns the existing opaque successor-context ref/digest required by the next request without adding a receipt or changing revision
- **AND** summary status and every non-owner response remain plaintext-free and omit both context keys

#### Scenario: Terminal rollback and retirement remain reachable

- **WHEN** a completed cutover terminal returns a `plan-materialize` context, the owner materializes/renders/approves respectively a rollback or retirement plan, and passes approval's returned context into `rollback(terminal-plan)` or `retire-source(clearance)`
- **THEN** each action resolves its protected approval predecessor and token facts server-side, while retirement clearance returns the distinct lifecycle ref needed for external consumption/finalize
- **AND** neither path accepts caller-supplied predecessor/publication/authority facts or relies on status minting a new context

### Requirement: Black-box parity covers MCP, REST, CLI, and Hosted

Contract E2E SHALL invoke `consolidate_memory` through a real stdio MCP client,
authenticated REST application, installed CLI, and an explicitly selected
`hosted-alpha-agent-v3` cell/control-plane
adapter. Each surface SHALL expose the same finite actions, selector
classification, normalized schema, logical results, stable errors, idempotent
mutation terminals, fresh principal/session binding, and trusted-confirmation
semantics. The suite SHALL exercise at least one full lifecycle through stdio
MCP and one through Hosted, with REST/CLI parity probes at every externally
observable state and action boundary.

Generated OpenAPI, MCP schema, v3 Hosted descriptor/hash and plugin/manifest,
CLI help, bootstrap, and capability documentation SHALL be checked against
committed bounded fixtures. V1/v2 membership, descriptors, hashes,
plugins/manifests, locks, clients, and registered evidence SHALL be asserted
byte-identical, and the test SHALL prove that defining v3 neither selects nor
promotes it. V3 startup SHALL consume a valid signed private
`HostedProfileSelection/v1` whose typed cell/vault/installation-generation-fence,
release/protocol, descriptor,
Records/identity/run/seal/receipt reader, artifact store, source-export/control-
receipt verifier, archive-custodian verifier, owner-entitlement verifier,
owner-confirmation, exact-cell transport
supervisor, and rollback/recovery readiness tuple matches the running image.
The record SHALL be detached Ed25519-signed over its exact framed JCS bytes,
name its `ed25519-sha256:` signer key id, and verify through the private
purpose/audience/profile-scoped selection registry with status, validity,
revocation, generation, and bounded rotation overlap. Missing, unsigned,
unknown-key, revoked, expired, or one-field-mismatched records SHALL leave v1/v2
behavior unchanged and v3 unavailable. Every process and request SHALL have a timeout and
SHALL fail rather than hang. The gate SHALL assert that caller fields cannot forge owner
confirmation, destination identity, principal attestations, artifact trust, or
internal consolidation authority.

Generated request and terminal fixtures SHALL cover every action and conditional
branch as the closed tagged union. Each successful mutation terminal SHALL have
exact keys/types/requiredness for action, branch, run/revision, operation id,
request/result digests, logical `outcome=committed`, artifact digests, bounded
counts, ordered closed-enum `next_actions`, and branch-specific
`trusted_outputs`; `status` SHALL use `outcome=observed`. Every success SHALL be
the exact closed object `{"success":true,"data":{"delivery":D,"terminal":T}}`,
where `D` is one of the exact strings `initial` and `replayed` and `T` is the logical terminal;
top level and `data` each forbid every other key. Extra, missing,
null-forbidden, wrong-branch, or
unknown output fields SHALL fail fixtures across all four surfaces. Status SHALL
always use `delivery=initial`; only adoption of a stored mutating-operation
terminal MAY use `delivery=replayed`.

#### Scenario: Same sealed read crosses all surfaces

- **WHEN** equivalent owner and delegated content requests are made through MCP, REST, CLI, and Hosted during publication or recovery
- **THEN** every surface returns the same logical content-free sealed outcome with no phase/item/policy oracle
- **AND** transport wrappers do not add a path, count, title, snippet, run fact, or distinguishing error

#### Scenario: Identical mutation retry crosses a transport boundary

- **WHEN** acknowledgement is lost separately for each mutating action and conditional branch, including every render step and both retirement phases, and the same authenticated owner, operation id, and canonical tagged request is retried through an allowed equivalent adapter
- **THEN** initial delivery is exactly `{"success":true,"data":{"delivery":"initial","terminal":T}}` and replay is exactly `{"success":true,"data":{"delivery":"replayed","terminal":T}}`, with byte-identical canonical `T`, `outcome=committed`, and no second effect
- **AND** changed tenant, principal/session, action, token, artifact, plan, or payload conflicts

#### Scenario: Hosted deployment remains on v2

- **WHEN** v3 artifacts exist but the cell deployment explicitly selects v2
- **THEN** Hosted discovery and dispatch omit `consolidate_memory` and the v2 descriptor/hash remain unchanged
- **AND** the command becomes available only after a separately compatible v3 candidate is explicitly selected and its promotion evidence verifies

#### Scenario: Hosted apply drains its own cell

- **WHEN** v3 apply is admitted while ordinary Hosted mutations are active
- **THEN** the exact apply operation atomically converts out of the ordinary active-mutation counter before seal drain and waits for every other participant
- **AND** it completes without self-deadlock, double admission, or excluding another operation

#### Scenario: Hosted v3 selection trust is incomplete

- **WHEN** the selection signature algorithm/encoding, signer key, registry purpose/audience/profile, validity/revocation, source-export or custodian verifier readiness, owner-entitlement readiness, or exact-cell transport-supervisor readiness is absent or mismatched
- **THEN** startup neither advertises nor admits `consolidate_memory` for that cell
- **AND** v1/v2 descriptors, dispatch, and active deployment selection remain byte-identical

### Requirement: Crash-matrix E2E proves sealed recovery, abort, and truthful rollback

The gate SHALL inject process termination or deterministic failure after each
cross-store step—intent fsync, prepared-journal fsync, effect, exact
classification, terminal fsync, and final-journal fsync—for start, intake, each
snapshot, reconcile, plan/render/approval, approval reservation, seal intent,
seal drain, preimage completion,
restrictive-policy prepare/activate/receipt terminal, every content-batch
prepare/commit/receipt terminal, derivative rebuild, every verification class,
verified receipt, transport stop/each black-box probe/transport terminal/routing
open, abort restore, every rollback-plan/render/approval/restore/rebuild/probe
effect, and every retirement-plan/clearance/consume/completion/finalize effect.
Where an effect completes the product action, the matrix SHALL also inject after
its action-level idempotency-terminal fsync.
The retirement seams SHALL separately include forward-snapshot verification,
each surviving-copy-ledger calculation, pending-forward-only fence installation,
expiry/non-consumption fence release, and the retirement-finalize target that
permanently converts the fence.
For every successor-producing effect, the matrix SHALL additionally crash after
the inert seed/ref reservation but before intent, after terminal but before
full-context journal final, and after final but before logical-terminal storage.
It SHALL pin the seed and full-context fixed vectors and prove the receipt
terminal payload digest depends on the predecessor-free seed while the full
context depends on that already-final terminal, never the reverse.
After each injection it SHALL restart the installed server, prove the seal and
routing-stop loaded before ordinary admission, run exact recovery, and assert
one logical mutation and one outer `receipt/v1` intent/terminal pair per effect.
Each intent SHALL retain a 64-hex outer id, each terminal SHALL use the matching
`:committed|:aborted` suffix, and each record SHALL carry exactly one closed
nested `exomem.consolidation-event/<kind>/v1` payload. The suite SHALL assert
physical outer `prev` equals the actual local receipt head, nested semantic
parent follows the effect table independently, and journals reference outer
ids/hashes/sequences plus nested payload digests rather than conflating either
chain.

#### Scenario: Successor context is reconstructed after terminal crash

- **WHEN** the installed process crashes at each seed, receipt-terminal, context-final, and idempotency boundary for every shared-table producer and every review/executor context producer
- **THEN** restart derives one byte-identical full context from the reserved seed plus exact committed terminal and makes it current only in journal final
- **AND** no receipt contains the full context commitment, no second semantic event or revision appears, and replay returns the same logical terminal

The suite SHALL prove abort only before the first content batch, rollback after
that publication boundary, full preimage restoration without receipt rewind,
and successful derivative rebuild. It SHALL exercise the typed seal union and
prove a deletion-sealed cell cannot be reopened by consolidation recover or
generic resume. It SHALL also complete a cutover, unseal,
make a new unrelated canonical destination write, and prove an implicit or stale
rollback request
returns `ROLLBACK_RECONCILIATION_REQUIRED` without changing that write; only a
new `exomem.consolidation-rollback-plan/v1` whose union inventory gives every
later write and imported object an exact reviewed treatment, survives complete
trusted rendering, and receives its own single-use approval may proceed.

#### Scenario: Crash occurs at each durable seam

- **WHEN** the crash matrix restarts from every enumerated seam
- **THEN** exact prior/prepared/final states converge idempotently and mixed/third states stay sealed for owner repair
- **AND** no semantic decision, token consumption, policy activation, content batch, or receipt terminal is duplicated

#### Scenario: Later destination work exists

- **WHEN** post-cutover rollback is requested after the destination was unsealed and received a new canonical write
- **THEN** E2E observes `ROLLBACK_RECONCILIATION_REQUIRED`, the later write remains byte-identical, and no restore batch starts
- **AND** rollback succeeds only after a newly generated and separately confirmed exact reconciliation includes that drift

#### Scenario: Sealed restart reaches owner recovery only

- **WHEN** the process restarts in publication or transport verification while generic Hosted dispatch is sealed
- **THEN** only the authenticated owner-control lane can reach status, recover, abort, rollback, verify, or the exact apply resume and reconstruct phase-bound in-process authority
- **AND** non-owner, ordinary content, new-apply, and deletion-seal bypass attempts remain content-free and closed

#### Scenario: Both rollback branches remain reachable and disjoint

- **WHEN** the matrix first requests `rollback_mode=nonterminal-contingency` for an eligible sealed apply and later requests `rollback_mode=terminal-plan` for a completed run
- **THEN** the first branch's opaque context resolves server-side to the exact original apply journal/publication state/predecessor/reserved contingency authority and its body forbids those fields plus a rollback token, while the second requires its separately rendered plan/token and matching context and forbids contingency fields
- **AND** restart, lost acknowledgement, a mode change under the same operation id, or mixed branch fields never duplicate or widen either rollback

#### Scenario: Physical and semantic receipt parents diverge safely

- **WHEN** another valid receipt is appended between two causally adjacent consolidation effects
- **THEN** the later outer record chains from that actual physical hash while its nested payload names the declared prior consolidation terminal and digest
- **AND** recovery accepts the valid ancestry but rejects a caller-selected, skipped, duplicated, or reordered semantic parent

### Requirement: Disclosure E2E proves both permitted utility and absent private state

The installed product gate SHALL execute the approved representative
principal-by-purpose-by-item matrix through the in-process adapter/serializer
pipeline during the seal and through real black-box transports first on the
exact cutover cell during `transport-verifying` with public routing stopped, then
after routing opens for persistence parity.
The in-process authority SHALL never be serialized into a black-box request.
Positive assertions SHALL cover full owner access, every
allowed delegated domain, and only explicitly approved abstractions. Negative
assertions SHALL cover private bodies, source-only provenance, withheld
relations/history/media/Record items, wrong purpose, stale or cross-session
authorization, raw reads, enumeration/counts, resources, success/error shapes,
and bounded timing differentials. Receipt assertions SHALL prove content-free
schemas and exactly-once critical evidence without making receipts policy input.

The gate SHALL also inspect generated bootstrap/help, verification summaries,
and retirement output for the explicit limit that direct filesystem access,
manual copy/paste, direct object-store access, and external-model upload outside
Exomem are not release-gated. It SHALL not perform such external bypasses and
then report them as governed coverage.

#### Scenario: Private item differs only by existence

- **WHEN** each ordinary surface is tested with an absent item and a present-but-private item under sealed and post-unseal denied states
- **THEN** content, metadata, counts, error shape/length, and bounded timing are indistinguishable
- **AND** receipts contain decisions/fingerprints/counts but no body, path, title, reference, raw principal, or policy text

#### Scenario: Allowed abstraction is useful but bounded

- **WHEN** an attested delegated principal requests an approved purpose/domain whose policy allows only a compiled abstraction
- **THEN** every in-process pre-unseal adapter projection and every normal post-unseal black-box surface returns that exact approved projection and no underlying private body or source-only provenance
- **AND** pre-unseal truth does not depend on serializing internal authority across a transport

#### Scenario: Exact-cell transport probe fails closed

- **WHEN** a normal-auth MCP, REST, Hosted, or CLI negative/positive probe fails or its bound census/build/profile/config/trust/principal-mapping/routing proof drifts while public ingress is stopped
- **THEN** routing never opens and restart re-seals the exact cell or admits only owner recovery/rollback
- **AND** a clone result or special test principal cannot replace the missing exact-cell evidence

### Requirement: Operational E2E separates rehearsal, cutover, and retirement authority

The release gate SHALL run a full `cloned-rehearsal` against explicit clone
bindings with new active vault/installation ids plus immutable clone-of
lineage, including representative probes, crash/retry coverage, and verified
rollback. It SHALL then use distinct temporary real-mode fixtures to prove that
rehearsal tokens, attestations, snapshots, and authority are rejected for
`real-cutover`; fresh real snapshots/attestations/plan and a second owner
confirmation are required. After successful cutover, source-retirement
clearance SHALL remain unavailable until a third retirement-specific
confirmation and every current retention/recovery prerequisite verifies.
The retirement E2E SHALL bind an exact archive retention, trusted-custodian
transfer, or external-destruction disposition and SHALL assert the preview names
which destination bytes, owner-only mappings/digests, and plaintext-free
receipts remain versus which source-only/archive-only provenance becomes
irrecoverable after destruction.

The retirement E2E SHALL select and verify one post-retirement rollback mode.
`pre-cutover-reversible` SHALL retain and revalidate a source/archive copy for
the declared window. Before any source/archive copy may become irrecoverable,
`forward-only` SHALL retain a separately verified post-cutover forward snapshot
covering every imported byte and provenance bundle, persist a per-bundle
surviving-copy ledger, persist a pending pre-cutover-rollback fence before
clearance issuance, and permanently reject pre-cutover rollback after finalize. Clearance
SHALL be consumed once under current source lifetime/fencing authority before an
external destructive step; issuance, consumption, external completion, and
destination finalization SHALL be independently retried and evidenced.

A custodian-transfer fixture SHALL use the exact detached
`archive-custody-receipt/v1` claims and framed-JCS Ed25519 signature plus a
private `ArchiveCustodianVerifierRecord/v1`. The gate SHALL independently
revalidate custodian/retention domain and terms, transfer operation,
archive/manifest/source-census digests, signer status/validity/revocation, and
artifact availability at retirement plan, clearance, source consumption,
rollback plan, and rollback commit. Valid, expired, revoked, wrong-domain,
wrong-artifact, missing-history, and two-key-overlap vectors SHALL be fixed.

Capability shipment and its green E2E SHALL NOT access, consolidate, route,
retire, or delete an operator's real source or destination. The test/report
language SHALL distinguish reusable capability proof from a later operational
rehearsal, real cutover, and source-side retirement.

#### Scenario: Rehearsal completes and rolls back

- **WHEN** the clone-bound run completes apply, verification, crash recovery, and full rollback
- **THEN** its proof satisfies only the rehearsal prerequisite
- **AND** none of its tokens, snapshot fingerprints, attestations, or internal authority can authorize a real cutover

#### Scenario: Cutover succeeds but retirement is unconfirmed

- **WHEN** the separately confirmed temporary real-mode cutover is verified but no retirement-specific confirmation exists
- **THEN** retirement clearance is refused and the source fixture remains intact
- **AND** completion output does not imply routing shutdown, deletion, backup/key destruction, or account/billing changes

#### Scenario: Archive-destruction disposition is reviewed

- **WHEN** the temporary retirement plan selects external archive destruction
- **THEN** the distinct confirmation binds that disposition and the exact retained-versus-irrecoverable provenance statement
- **AND** E2E leaves actual archive destruction to the external operator and verifies C1 owner-only mappings remain

#### Scenario: Retirement would remove the final imported copy

- **WHEN** archive destruction is proposed without a verified forward snapshot or any imported byte/provenance bundle has zero surviving copies after the effect
- **THEN** clearance and source-lifecycle consumption both refuse before an external destructive step
- **AND** a pre-cutover destination preimage is not counted as a copy of imported data

#### Scenario: Retirement clearance is consumed and replayed

- **WHEN** the source operator consumes the exact unexpired JTI under the current checkpoint, disposition, destination no-loss proof, and fence, then retries or changes one field
- **THEN** the identical retry returns its consume terminal while replay or drift authorizes no second/destructive effect
- **AND** only a separate authenticated external completion attestation permits destination finalization and conversion of the already-active pending fence into the permanent rollback disposition

#### Scenario: Forward-only completion acknowledgement is lost

- **WHEN** the source consumes forward-only clearance and may have destroyed its copy but destination completion/finalization acknowledgement is lost
- **THEN** restart retains the pre-issued pending rollback fence and refuses every pre-cutover target
- **AND** recovery cannot release the fence unless source evidence proves the JTI was never consumed and the unchanged source/archive still survives

#### Scenario: Custodian trust changes between gates

- **WHEN** a valid custodian receipt is revoked, expires, changes retention terms, or loses its verified archive before clearance, consume, rollback planning, or rollback commit
- **THEN** that gate recomputes the surviving-copy ledger without the custodian copy and refuses before effect when any imported bundle would reach zero copies
- **AND** cached validation, prior owner approval, or byte-identical receipt claims do not override current verifier state
