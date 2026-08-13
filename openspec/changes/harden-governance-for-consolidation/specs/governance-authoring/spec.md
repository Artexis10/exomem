## MODIFIED Requirements

### Requirement: Natural-language propose and validated commit

The client/LLM SHALL interpret natural-language user intent and supply structured
canonical documents and arguments; Exomem SHALL validate, compile, and resolve
exact membership and policy facts. It SHALL NOT perform server NLP or query
planning. `govern_memory(operation="propose")` SHALL return the interpretation,
canonical policy, resolved affected membership, consequences, overlaps, duration,
reversal path, and a single-use proposal id. Samples, privacy, consequences, and
overlaps SHALL derive solely from current plus prospective compiled policy and
concrete membership. `selector_paths` and `target_ceiling` are compatibility hints
only: a mismatch SHALL be diagnostic or rejected and SHALL NOT determine privacy,
direction, or commit identity. The membership preview SHALL render each affected
item at its current effective disclosure ceiling and SHALL NOT leak titles or
excerpts of items that a not-yet-committed rule would restrict.

The proposal SHALL bind the exact no-follow workspace byte map/source fingerprint,
conflict digest, pending-guard generation and regular-file identities; current active
policy-generation/fingerprint, projector-version, and catalog-generation tuple; target
canonical compiled bytes/fingerprint and compiler/projector versions; exact affected-
item manifest; and required ready authorization-projection namespace for that catalog.
Mutable `_Governance` documents SHALL be the
reviewable authoring workspace and pending history, not active authority by themselves.

`govern_memory(operation="commit")` SHALL reserve the proposal id for one deterministic
operation id, allow only that event to retry, and mark it spent exactly once when its
immutable target generation becomes active. Exact-prior abort before an authoritative
transaction SHALL release the reservation. Commit SHALL revalidate the expected active
tuple and affected manifest, then use one SQLite `BEGIN IMMEDIATE` transaction in the
private no-follow governance activation store to insert the complete append-only
compiled generation and compare-and-swap `active_governance_tuple` from the reviewed
predecessor to `(target generation/fingerprint, target projector version, expected
catalog generation)`. Tuple, receipt activation state, activation epoch/digest, and ready
projection-namespace reference SHALL commit atomically in the activation store. The
external registry SHALL then CAS its expected id/epoch/digest to those exact committed
values; reads are BLOCKED during mismatch and recovery may acknowledge only that receipt-
proven tuple. The SQLite transaction SHALL be the
sole authority-publication linearization point. Every request SHALL snapshot the tuple
once and use only its complete immutable policy/catalog/namespace; mutable source state,
a final filesystem check, independent catalog pointer, or workspace mirror MUST NOT
select authority.

Active-tuple or affected-membership drift before the tuple transaction SHALL refuse
stale and require a fresh proposal unless durable receipt evidence already proves the
exact target transaction committed. Drift after target preparation or committed
evidence SHALL remain BLOCKED only when needed to determine/finish that exact tuple
outcome; reconciliation SHALL NOT recompile current workspace, replay interpretation,
or select a different target. A crash before SQLite commit exposes the predecessor and
may release the nonce after exact abort; a crash after commit exposes the complete
target and reconciliation completes only its terminal evidence.

The reviewed target documents SHALL be mirrored into `_Governance` separately under a
cooperative writer fence, through held-parent no-follow traversal and descriptor-identity
checks against the proposal's captured workspace bytes. An observed byte or identity
drift SHALL refuse the mirror and remains pending next-generation input with owner-only
source/generation conflict diagnostics. Direct OS-owner mutation is outside the
cooperative writer fence. A workspace edit cannot alter the immutable target or become
active, and mirror failure alone need not abort an otherwise exact tuple transaction. The
receipt SHALL record active generation, prior/target source identities, and mirror
outcome. Source/generation parity and recovery SHALL recompile only the exact source
bytes stored in the immutable generation; no recovery path may infer active authority
from a fresh workspace walk.

On successful tuple activation, commit SHALL archive the predecessor generation,
publish the new policy fingerprint on the next request, and preserve any independently
edited workspace bytes for a future proposal. Backup/restore and rebuild SHALL require
the WAL-consistent activation store, receipts, compiler version, and exact immutable
source snapshot; mutable `_Governance` alone is insufficient.

#### Scenario: Propose does not leak restricted members

- **WHEN** a proposal would restrict N pages and the preview lists affected
  membership
- **THEN** counts and current-ceiling samples are returned, but no title or
  excerpt of a would-be-restricted page crosses the boundary

#### Scenario: Caller hints cannot change a proposal's facts

- **WHEN** selector or ceiling hints disagree with compiled prospective membership
- **THEN** the proposal diagnoses or rejects the mismatch and derives its preview,
  direction, and commit identity from compiled policy and concrete membership

#### Scenario: Commit consumes the nonce once

- **WHEN** a proposal id is committed and then committed again
- **THEN** the first commit writes the policy and the second is refused as spent

#### Scenario: Exact-prior commit refuses on drift

- **WHEN** the active compiled generation or affected item manifest changes after propose
  while no authoritative tuple transaction committed
- **THEN** commit refuses with a stale-policy error, activates no target policy,
  retains only any receipt-first aborted evidence, and requires a fresh proposal

#### Scenario: Prepared proposal drift blocks without reinterpretation

- **WHEN** affected membership drifts after target preparation or receipt evidence exists
  but the active-tuple transaction outcome is uncertain
- **THEN** the event remains blocked and unspent until exact restoration or explicit
  transaction reconciliation, and it never recompiles/reinterprets a different target

#### Scenario: Observed workspace drift remains pending

- **WHEN** held-parent/descriptor checks observe a reviewed policy file changed after
  snapshot acquisition before or after active-tuple publication
- **THEN** the mirror refuses; the exact reviewed generation may activate if active-base/
  membership checks pass, and the observed edited bytes remain pending input with
  owner-only divergence diagnostics. Direct OS-owner mutation is outside cooperative
  fencing

#### Scenario: Atomic active tuple transaction is the concurrency cut

- **WHEN** the immutable generation, receipt activation, ready projection namespace, and
  compare-and-swap active tuple commit in one SQLite transaction
- **THEN** every reader observes the complete predecessor or target and never a hybrid;
  filesystem timing does not define the cut

#### Scenario: Competing commits serialize on the active generation

- **WHEN** two proposals try to activate different targets from the same predecessor
- **THEN** at most one active-tuple compare-and-swap commits, the other refuses stale, and no
  immutable generation is overwritten

#### Scenario: Policy commit races content or companion publication

- **WHEN** a policy commit expecting catalog C races a create/edit/delete/media or
  companion commit expecting policy P from the same active tuple
- **THEN** one full tuple CAS wins, the other refuses/rebuilds against it, and neither
  publishes a policy/catalog pair without its exact ready projection namespace

#### Scenario: Crash after nonce reservation does not spend the proposal

- **WHEN** commit reserves the proposal but crashes while authoritative policy
  still points to the exact prior generation
- **THEN** reconciliation aborts the operation and releases the reservation;
  another event cannot steal it, and the user may retry the proposal

#### Scenario: Crash after active tuple commit completes exact target

- **WHEN** the process crashes after the SQLite active-tuple transaction commits but before
  terminal response or workspace mirror
- **THEN** recovery keeps that immutable target active, completes its receipt once, and
  retries the mirror only if expected workspace bytes still match

### Requirement: Suspend, resume, undo with coherent dependents

`govern_memory` SHALL support suspending and resuming a whole rule set and undoing the
last policy change, but none of those operations SHALL activate by restoring or editing
YAML. Each reviewed operation SHALL prepare one exact immutable target comprising:

- canonical compiled policy bytes and a new immutable policy generation with exact stored
  source-document bytes;
- the complete before/after dependent-grant state and a membership manifest resolved
  against the proposal's exact catalog generation;
- deterministic expiry/review findings for every grant whose scope, selector, member
  identity/hash, or policy fingerprint changes;
- the complete ready authorization-projection namespace for the target policy/projector
  and expected catalog; and
- the complete predecessor and target `active_governance_tuple` identities plus journal,
  receipt, and external activation epoch/digest evidence.

Direction SHALL be computed over the resolved before/after disclosure lattice, including
dependent grants, audiences, purposes, scopes, membership, and all levels/options. A
proven narrowing MAY install only its separately receipted fail-closed overlay after
durable intent. Widening or unknown direction—including suspending a restrictive rule—
SHALL keep the predecessor authority until publication. Neither direction may activate
the target generation or dependent-grant rows before terminal evidence.

Publication SHALL use one SQLite transaction to verify the exact predecessor policy/
projector/catalog tuple and dependent membership manifest, insert the immutable target
generation, atomically apply the exact target dependent-grant rows, and compare-and-swap
the single active tuple with its ready namespace, receipt state, and store-side activation
epoch/digest. A content create/edit/delete or companion publication racing the operation
SHALL make one full tuple CAS stale; it MUST NOT allow target policy/grants to activate
against a different catalog. Recovery SHALL finish only the exact recorded transaction
and external-registry acknowledgement, never re-resolve current YAML or a new catalog.

For undo, the archived predecessor's exact stored source bytes are immutable generation
input, not live authority. Restoring them into `_Governance` SHALL be a separate held-
parent mirror under the cooperative writer fence after/beside publication. An observed
byte or descriptor-identity drift refuses that mirror and leaves the bytes pending; mirror
failure SHALL NOT roll back, replace, or modify the published generation. Direct OS-owner
mutation is outside the cooperative writer fence. A partial, missing, conflicted, or
otherwise invalid enrolled workspace SHALL still make content serving BLOCKED until
repair, without changing the active tuple.

#### Scenario: Widening suspend waits for atomic publication

- **WHEN** suspending a restrictive rule would raise any effective decision after
  dependent grants are recomputed
- **THEN** the predecessor tuple/grants remain authoritative until committed terminal
  evidence and the complete target tuple transaction; no pending YAML or prepared row
  widens early

#### Scenario: Proven narrowing resume may overlay but not publish early

- **WHEN** resuming a restrictive rule is proven pointwise no more permissive across the
  exact membership/grant manifest
- **THEN** only the receipt-backed narrowing overlay may apply before publication, while
  the target generation/dependent grants activate together at the tuple CAS

#### Scenario: Undo expires or flags changed dependent membership

- **WHEN** an archived policy's selectors would give a dependent grant a different scope,
  member identity/hash, or member set in the reviewed catalog
- **THEN** the immutable target includes deterministic expiry/review state for that grant,
  and commit refuses if the manifest changes rather than activating policy over stale
  grant authority

#### Scenario: Content publication race makes one operation stale

- **WHEN** suspend, resume, or undo races content create/edit/delete or companion
  publication from the same predecessor tuple
- **THEN** exactly one complete tuple transaction wins; the loser requires fresh
  membership/namespace review and no policy/grant/catalog hybrid is visible

#### Scenario: YAML mirror failure is non-authoritative

- **WHEN** the exact target tuple commits but the cooperatively fenced `_Governance`
  mirror observes changed identities or fails
- **THEN** the committed immutable policy/dependent grants remain selected, observed
  divergent bytes remain pending, and invalid workspace state blocks serving until
  repaired; direct OS-owner mutation is outside the fence

#### Scenario: Crash before publication retains predecessor

- **WHEN** a crash occurs after target preparation, grant re-resolution, receipt intent,
  namespace build, or partial non-authoritative mirror but before the tuple transaction
- **THEN** recovery exposes no target policy/grant authority and may complete only the
  exact transaction if every predecessor tuple/manifest/evidence identity still matches

#### Scenario: Crash after publication completes exact target

- **WHEN** the tuple/dependent-grant/receipt transaction commits and the process crashes
  before external-registry acknowledgement, terminal response, or YAML mirror
- **THEN** serving remains BLOCKED until exact digest acknowledgement, then recovery
  retains that target once, completes evidence/mirror idempotently, and never replays the
  semantic suspend/resume/undo request

### Requirement: Pending YAML mutation blocks hybrid activation

Mutable `_Governance` YAML SHALL be a reviewable authoring workspace and pending source,
never active authority. Every authorization-affecting operation SHALL still create its
durable allocating/pending journal and plaintext-free marker before staged effects, but
the guard SHALL protect proposal, mirror, receipt, and tuple publication—not make a
partially written document set active. Direct manual create/edit/delete changes valid
pending input only. It SHALL NOT compile into live authority, revoke a deleted grant, or
replace the active tuple. A missing, conflicted, symlinked, unreadable, or unparsable
workspace in an enrolled vault SHALL fail ordinary content serving closed rather than
falling back to last-good or OPEN.

The exact staged target SHALL be an immutable compiled-policy generation plus a ready
projection namespace for the proposal's expected catalog. Activation SHALL occur only
when one SQLite transaction verifies journal/receipt evidence and compare-and-swaps the
complete `active_governance_tuple`. The marker and journal SHALL remain control metadata
outside logical policy composites and bind protocol, phase, event, prior/prepared/final
digests, affected ids/paths, complete expected tuple, and target tuple. Removing a marker
cannot bypass a pending journal. A workspace mirror is a separate cooperatively fenced,
held-parent operation; observed drift refuses it, and it MUST NOT define activation.

#### Scenario: Crash during multi-file mirror does not activate a hybrid

- **WHEN** a crash leaves only some reviewed target YAML mirrored into `_Governance`
- **THEN** no YAML subset becomes authority; the active tuple remains immutable and
  content serving is BLOCKED while the enrolled workspace is invalid or journal recovery
  is incomplete

#### Scenario: Direct YAML edit or deletion is pending only

- **WHEN** an external editor creates, changes, or deletes a conflict-free policy/grant
  document without a reviewed commit
- **THEN** the active tuple is unchanged, deletion does not revoke active authority, and
  owner diagnostics identify pending source/generation divergence

#### Scenario: Active tuple prevents policy catalog hybrid

- **WHEN** a policy operation and a content/companion writer race from one expected tuple
- **THEN** only one complete policy/projector/catalog tuple commits and the stale event
  cannot activate its prepared generation or mirror as an independent authority

### Requirement: Restart reconciliation never replays or guesses

Before accepting another governance-authoring write or serving enrolled content after an
open event, Exomem SHALL reconcile the journal/marker, immutable generation, active
tuple, receipt evidence, external enrollment record, and workspace integrity. It SHALL
use only exact prior/prepared/final identities recorded durably. It MUST NOT replay a
semantic request, re-run natural-language interpretation, compile a fresh workspace,
select a historical generation, or infer activation from current YAML.

An allocating event with no durable intent may close exact-prior. A pending event with
exact prepared immutable bytes and intact receipt chain may complete only its recorded
active-tuple CAS and authenticated external expected-id/epoch/digest acknowledgement.
A committed tuple may have its terminal evidence/mirror completed idempotently, but the
mirror runs only when the cooperative writer fence and captured workspace identities
still match; observed drift refuses the mirror. Any mixed/third state, missing required
terminal, registry/store mismatch, invalid workspace, or target tuple that no longer
matches the expected predecessor SHALL remain BLOCKED. Reconciliation makes no
filesystem guarantee against direct OS-owner workspace mutation.

#### Scenario: Exact prepared state completes only recorded tuple

- **WHEN** restart finds exact staged immutable target/namespace and durable intent but
  the active-tuple CAS did not commit
- **THEN** reconciliation may perform only that recorded CAS if the complete predecessor
  tuple still matches; otherwise it stays blocked without recompiling current YAML

#### Scenario: Tuple committed before external acknowledgement

- **WHEN** the SQLite active tuple and receipt state committed but the process crashed
  before the protected registry expected epoch/digest was acknowledged
- **THEN** reads are BLOCKED until reconciliation proves the exact committed row/digest
  and completes that same authenticated acknowledgement once

#### Scenario: Committed tuple survives observed mirror drift

- **WHEN** the target tuple committed and current workspace bytes differ from the mirror
  expectation
- **THEN** reconciliation keeps the immutable tuple and, when it observes the drift,
  refuses the mirror and treats valid bytes as pending; invalid/missing workspace keeps
  content reads fail-closed until repair. Direct OS-owner mutation is outside the fence

#### Scenario: Third-state recovery blocks

- **WHEN** journal, receipt, external registry, activation store, tuple, generation,
  catalog, namespace, or workspace matches neither exact prior, prepared, nor final
- **THEN** authoring and enrolled content remain blocked and recovery does not choose a
  last-good, historical, mutable-YAML, or OPEN fallback

### Requirement: Pending recovery state is pinned against TTL and GC

Every TTL or garbage-collection path SHALL exclude proposal, token, grant, purpose,
immutable policy/catalog generation, namespace, receipt, and dependent row referenced by
an open operation journal or active tuple. Logical expiry MAY make pending session state
non-authorizing, but physical deletion SHALL NOT destroy an exact recovery composite.
Activation or exact-prior abort SHALL close the journal; closed journals SHALL not govern
later live state, while immutable generations remain retained according to active-request,
cursor, receipt, backup, and rollback pins.

#### Scenario: Prepared grant survives expiry until reconciliation

- **WHEN** a crash leaves exact prepared token/grant state, time passes its TTL, and GC
  runs before reconciliation
- **THEN** referenced rows remain pinned, the expired grant authorizes nothing, and exact
  journal/tuple reconciliation remains possible

#### Scenario: Active tuple components cannot be collected independently

- **WHEN** GC encounters a policy generation, catalog, projection namespace, graph, or
  lane referenced by the active tuple or a live request/cursor
- **THEN** it retains the complete set and cannot leave a tuple whose named component is
  missing or stale

#### Scenario: Undo cannot activate over stale dependent state

- **WHEN** staged policy bytes are mirrored but any dependent grant/catalog/namespace
  component differs from the recorded target composite
- **THEN** reconciliation treats the operation as partial and keeps target activation
  blocked; mirrored YAML alone has no authority

### Requirement: Governance-tools migration is monotonic

The current sidecar schema is exact v3. Ordinary openers SHALL be non-migrating: every
v3 token, receipt, policy, and governance-tools opener SHALL leave `PRAGMA user_version`
at 3 and perform no v4 DDL/DML; a v4-capable ordinary opener encountering v3 SHALL return
`MIGRATION_REQUIRED` without changing it. An opener for v4 SHALL preserve exact v4, and
unknown versions above v4 SHALL refuse without writes. Only the explicit authenticated
offline migration coordinator under the cooperative whole-tree/schema/replica fence MAY
perform v3→v4.

That coordinator SHALL drain every v3 process, freeze and snapshot the exact v3 database,
receipts, workspace, raw indexes, and catalog, prove a stable conflict-free workspace,
commit irreversible external `governance_enrolled=true`, then transactionally create
bound authorization sessions, append-only policy/catalog generations, and one complete
active policy/projector/catalog tuple with its exact external store id/epoch/digest. It
SHALL serve no v4 request until the tuple and projection namespace verify. A crash after
enrollment but before completion is BLOCKED. Mixed v3/v4 service is forbidden by the
external schema/lease fence; no old-binary compatibility is claimed.

Rollback SHALL use the tested pre-migration v3 snapshot or the explicit offline v4→v3
tool. The latter closes v4 sessions and dependent authority, mirrors the active tuple's
exact stored source under a whole-tree fence, proves compile/catalog parity, removes only
v4 state, restores exact v3 schema and `user_version=3`, and advances external recovery
metadata before the real v3 binary starts. It MUST NOT set `governance_enrolled=false`;
the protected monotonic history remains enrolled, and an operator intentionally running
v3 relies on the deployment fence rather than the v4 OPEN path.

#### Scenario: Ordinary v3 opener leaves v3 byte-for-byte

- **WHEN** any v3 or v4-capable ordinary token, receipt, policy, or governance-tools path
  opens an exact v3 fixture outside the migration coordinator
- **THEN** `user_version` remains 3, no v4 table/index/row is created, and the fixture's
  database bytes change only as explicitly allowed by its pre-existing v3 read contract

#### Scenario: Explicit v3 to v4 migration initializes one active tuple

- **WHEN** the authenticated coordinator migrates a quiesced exact v3 fixture
- **THEN** it records irreversible enrollment and atomically creates the verified policy/
  projector/catalog tuple plus bearer-free session state, with crash injection proving
  predecessor snapshot or BLOCKED/complete-v4 outcomes only

#### Scenario: Old binary is fenced from v4

- **WHEN** the real pre-change v3 binary is probed against an isolated v4 copy
- **THEN** its exact behavior is recorded but the external schema/lease fence prevents it
  from joining or writing the live v4 cell regardless of self-refusal

#### Scenario: Downmigration does not erase enrollment history

- **WHEN** the offline v4→v3 path completes and the actual v3 binary starts
- **THEN** v4 session authority is closed, exact v3 schema/source parity is proven, and
  the external registry still records monotonic prior governance enrollment

## ADDED Requirements

### Requirement: Reserved Administration Trees Have Exclusive Owning Commands

`_Governance` SHALL be authorable only through the validated, receipt-first
`govern_memory` lifecycle. `_Consolidation` SHALL be reserved exclusively for the
consolidation command that owns its run state; until that command exists, no public
command SHALL mutate or enumerate it. Owner identity, L6 disclosure, Tier-2 enablement,
an idempotency key, a filesystem alias, or a caller-provided bypass flag MUST NOT grant
generic administration-tree authority.

Generic create, directory-create, edit, observe, replace, append, move, delete, recover,
dataset, Records, media, frame, upload/download/transfer, repair, and multiplexed alias
operations SHALL refuse before touching either tree. Read/list/search routes SHALL treat
them as structurally absent. Both source and destination SHALL be checked for moves;
trash source, requested restore target, metadata-derived original target, and every
recursive child SHALL be checked for recovery. Owning commands SHALL receive a private,
non-serializable authority from the dispatcher and SHALL remain subject to their normal
proposal, receipt, crash-recovery, principal, and projection contracts.

The dispatcher classifier is preflight only. Every generic leaf SHALL traverse and
mutate relative to held no-follow vault/parent handles through the leaf operation and
revalidate stable filesystem identity at the read/mutation operation against retained
anchors. Stable pre-existing and anchor-observable parent swaps, rename/link changes,
hard links, reparse/junction points, and bind aliases SHALL refuse reserved-tree access.
A platform without equivalent handle-relative primitives SHALL disable the affected
route. The private owning-command authority
MAY enter its tree only through the same safe traversal plus its receipt-first lifecycle.

#### Scenario: Generic write cannot author governance policy

- **WHEN** the owner uses any generic create, edit, append, replace, move, or alias route
  to target a file under `_Governance`
- **THEN** the operation refuses before filesystem mutation and directs the caller to
  the reviewed governance lifecycle

#### Scenario: Recovery cannot restore into a reserved tree

- **WHEN** a trash sidecar, explicit restore target, alias, or recursive child would
  restore into `_Governance` or `_Consolidation`
- **THEN** ordered preflight refuses before that entry is restored; prior durable saga
  entries reconcile from recorded receipt/journal state; recovery remains per-entry
  rather than transactional across its full set

#### Scenario: Dataset and media leaves cannot reach administration state

- **WHEN** a generic dataset, Records, media, frame, transfer, or repair leaf targets a
  reserved tree directly or through an alias
- **THEN** it returns no bytes, rows, frames, metadata, count, or mutation effect

#### Scenario: Govern memory retains exclusive policy authority

- **WHEN** a valid owner-approved governance commit passes proposal, fingerprint,
  receipt, and activation guards
- **THEN** its private dispatcher authority may update `_Governance` and no generic
  public flag can reproduce that authority

#### Scenario: Consolidation root is reserved before its owner exists

- **WHEN** an ordinary command targets `_Consolidation` before `consolidate_memory` is
  implemented
- **THEN** the path remains unavailable and no fallback writer claims it

### Requirement: Governance Proposals Preserve Prospective Snapshot Identity

Every `govern_memory(operation="propose")` result SHALL consume a successful guarded
prospective compile and persist its exact workspace byte map/source fingerprint,
conflict-set digest, pending-guard generation and regular-file identities; reviewed
active policy-generation/fingerprint, projector-version, and catalog-generation tuple;
canonical target compiled bytes/fingerprint and schema versions; exact affected
membership; and required projection namespace. It SHALL NOT
reconstruct any identity from caller hints, a second unguarded walk, or mutable workspace
state during recovery.

Commit SHALL revalidate the complete active tuple and membership through the one SQLite
transaction that inserts the immutable compiled generation and compare-and-swaps the
active tuple. That transaction, not a filesystem check or mirror, SHALL be the
concurrency cut. Workspace mirroring SHALL instead use the cooperative writer fence,
held-parent no-follow traversal, and descriptor-identity checks; observed drift refuses
the mirror. Direct OS-owner mutation is outside the cooperative writer fence.
An observed external edit overlapping or following snapshot acquisition SHALL remain
pending future input and cannot alter or become part of the reviewed generation.

#### Scenario: Proposal refuses conflict instead of previewing it away

- **WHEN** any supported conflict copy is present in the live governance tree
- **THEN** propose returns the conflict refusal, stores no reviewable proposal, and
  creates no generation, receipt, or marker state; any existing activation store/tuple
  remains byte-for-byte unchanged

#### Scenario: Target fingerprint is part of review identity

- **WHEN** two prospective document sets share a live base but compile to different
  target fingerprints
- **THEN** their proposal identities differ and one proposal cannot commit the other's
  target

#### Scenario: Commit compares the guarded prospective identity

- **WHEN** any active tuple field, affected membership, target compiled bytes/fingerprint,
  compiler version, or ready projection namespace differs from the reviewed proposal
- **THEN** commit follows the existing stale/blocked recovery contract and does not
  reinterpret or activate a new target

#### Scenario: Prospective identity survives through tuple publication

- **WHEN** an external writer changes workspace bytes after prospective acquisition but
  the reviewed active tuple and affected membership still match
- **THEN** the tuple transaction may activate only the stored immutable target; when the
  cooperative fence observes the drift, the mirror refuses and diagnostics mark it
  pending next input. Direct OS-owner mutation is outside the cooperative writer fence

#### Scenario: Source parity uses immutable generation bytes

- **WHEN** doctor, recovery, backup verification, or rebuild checks the active policy
- **THEN** it recompiles the exact source byte map stored with the tuple's policy generation and
  requires identical canonical compiled bytes/fingerprint without trusting workspace

### Requirement: Companion Backfill Is Owner-Reviewed And Receipt-First

`govern_memory(operation="backfill_companion")` SHALL be an owner-only generated
proposal/commit lifecycle. Version-1 input SHALL contain exactly artifact class,
canonical artifact path, expected artifact SHA-256/size, expected companion path/SHA-256,
complete explicit artifact `semantics` lists (`projects`, `tags`, `types`, `classes`),
and all class-specific media/dataset/frame binding fields. The trusted adapter SHALL
establish the local owner; a session capability alone, model text, companion-page
metadata, artifact metadata, policy, dataset row, or media job MUST NOT authorize or
populate the input.

Preview SHALL show the exact target descriptor and immutable identities without treating
them as active classification. Commit SHALL revalidate every artifact/companion/parent
identity through handle-relative no-follow traversal, write only the descriptor under the
receipt-first mutation protocol, preserve all transcript/body/page metadata and processing
state, and record owner, proposal, prior/target hashes, and terminal result. It SHALL infer
neither non-empty nor explicitly empty semantics. Exact retry of the same committed event
SHALL be idempotent; changed semantics or identities SHALL require a new review.

For legacy scene frames, the reviewed descriptor SHALL use bounded integer
`frame_timestamp_ms` and the exact filename/index/float conversion defined by
`governance-kernel`; any mismatch SHALL refuse atomically.

#### Scenario: Page tags cannot authorize artifact semantics

- **WHEN** a legacy companion carries page-level tags/projects/type/classes but the owner
  supplies no matching explicit artifact semantics input
- **THEN** preview/commit refuses incomplete input and does not infer or write a
  governance companion descriptor

#### Scenario: Backfill writes only the reviewed descriptor

- **WHEN** exact owner input commits against unchanged artifact/companion identities
- **THEN** one receipt-backed descriptor is added while body, transcript, page metadata,
  processing state, and artifact bytes remain unchanged

#### Scenario: Backfill drift or crash cannot partially classify

- **WHEN** any bound bytes drift or a crash occurs at any receipt/mutation boundary
- **THEN** recovery yields exact prior or the one reviewed descriptor with one terminal,
  never a partial tuple, inferred semantics, or mixed frame timestamp

### Requirement: Session-Scoped Authoring Uses Verified Request Context

The governance session lifecycle SHALL expose generated `open`, `status`, `rotate`, and
`close` actions through `govern_memory(operation="session")`. It SHALL resolve the
principal from the trusted surface context and SHALL issue or verify the server-side
authorization-session capability defined by `authorization-session-binding`. No
retrieved text, policy document, natural-language intent, caller-supplied principal, or
arbitrary legacy handle SHALL establish that context.

`grant`, session-scoped `revoke`, and `declare` SHALL require a verified active internal
session bound to the resolved principal and issuer family before reading or changing
authorization state. Grant redemption SHALL additionally bind the exact scope IDs from
the reviewed token membership; it MUST NOT widen to other current item scopes. Session
close and revoke SHALL use the stable internal session ID and SHALL never persist or
receipt the raw bearer.

#### Scenario: Session open is an explicit control action

- **WHEN** a resolved caller invokes the generated session `open` action
- **THEN** Exomem creates the bounded principal/issuer binding and returns the bearer
  only in the exact typed successful `issued_credential` field after terminal validation

#### Scenario: Caller-selected principal is rejected

- **WHEN** grant, session revoke, declare, or session lifecycle input attempts to supply
  a principal, principal scope, issuer, audience-for-self, or internal session id
- **THEN** validation refuses before token consumption, sidecar mutation, receipt, or
  journal allocation

#### Scenario: Session grant binds exact reviewed scopes

- **WHEN** a token for an item in scope A is redeemed while the item also belongs to
  scope B
- **THEN** the session grant records only the token's reviewed scope bindings and cannot
  raise B

#### Scenario: Session capability never enters evidence

- **WHEN** session lifecycle, grant, revoke, or declare appends durable evidence
- **THEN** receipts and journals contain the stable internal session identity/digest as
  needed but never the raw bearer
