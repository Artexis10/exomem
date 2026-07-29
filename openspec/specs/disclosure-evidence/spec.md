# disclosure-evidence Specification

## Purpose
Provide durable, plaintext-free, tamper-evident evidence for governed egress,
credential and token lifecycle, and governed deletion/recovery while keeping
ordinary ungoverned recall state-free.

## Requirements

### Requirement: Proportional plaintext-free receipts

Governed egress, withhold-token lifecycle, and governed content lifecycle events
SHALL be recorded as receipts, while ordinary ungoverned L6 recall SHALL produce
no receipt. A credential block from the always-on scrubber SHALL count as a
governed decision even without policy files and MAY create only the reserved
events subtree and sidecar while policy remains disabled.

Every record SHALL use a versioned event-type schema. The common envelope SHALL
require `schema`, stable `event_id`, `event_type`, `phase`, timestamp, machine
instance, sequence, `prev`, and `hash`. A critical event id SHALL be
deterministic from its operation identity. A read boundary SHALL mint a fresh id
once per top-level invocation and retain it only for internal append retries.
Required payloads SHALL be:

- disclosure: top-level boundary id/type plus an `outcomes` array whose typed
  members carry only applicable refs/content hashes/byte sizes, decision or
  level, redaction/count summaries, principal/audience/purpose, policy
  fingerprint, confirmation type, and scope ids/keyed label digests;
- credential block: boundary metadata, principal/audience when known, and
  redaction/count summaries, with policy/scope fields optional because no policy
  need exist;
- token mint/redeem: token-id digest, bounds fingerprint, and causation id, never
  token bytes;
- deletion/recovery: manifest digest, affected refs/content hashes, exact-state
  digest, and causation id; and
- critical intent/terminal: operation, prior and final-target composite digests,
  an exact prepared digest when the caller uses staged activation, affected
  content-free ids, and outcome.

Mixed-level or aggregate responses SHALL use multiple typed `outcomes` in the
one boundary event, not fabricate a single level. No schema SHALL permit
released content, credentials, human scope labels, or plaintext logging.

#### Scenario: Ungoverned recall writes nothing

- **WHEN** a query runs at L6 on a vault with no policy and the credential
  scrubber does not intervene
- **THEN** no receipt is written

#### Scenario: Governed release is receipted without plaintext

- **WHEN** an item is released below full to an audience
- **THEN** a receipt records the refs, hashes, sizes, level, audience, and policy
  fingerprint, and contains neither the released text nor the scope's label text

#### Scenario: Credential blocking is receipted without policy

- **WHEN** the always-on scrubber blocks a credential-shaped value in an
  otherwise ungoverned vault
- **THEN** the block is receipted without recording the credential or released
  plaintext

### Requirement: Exactly-once decision emission at the final representation

Each successful governed top-level boundary SHALL collect content-free
disclosure outcomes where reductions/projections occur and SHALL emit exactly
one logical receipt after its final representation has been selected. Nested
aliases SHALL contribute outcomes to the request-scoped collector and SHALL NOT
emit before the outer representation is finalized. An internal append retry
within that invocation SHALL reuse its minted id; a new external invocation
SHALL mint a new id. The universal terminal postfilter SHALL remain
side-effect-free because some transports invoke it more than once, and the
second MCP filter pass SHALL emit zero events.

Coverage SHALL be derived from content-returning operation/mode branches and
registered reduction adapters, not only top-level command names. Adding a new
content-returning branch inside an existing product command SHALL fail coverage
until it declares an outcome adapter. Download, frame, and prompt/resource
routes outside that registry SHALL have explicit coverage. A streaming
authorization receipt SHALL say `release_authorized` and SHALL NOT claim
transport delivery.
The receipt append SHALL complete before returning a governed representation.
If append reports an error, the boundary SHALL fail closed with a content-free
retryable service error and SHALL NOT return the unreceipted payload. Read-path
events need not fsync and SHALL make no whole-system power-loss durability claim.

#### Scenario: MCP's second filter pass does not duplicate evidence

- **WHEN** an MCP response runs both the universal dispatcher filter and the MCP
  filter pass
- **THEN** the governed decision produces one receipt, not two

#### Scenario: Aggregate withholding is receipted where reduced

- **WHEN** overview or structure output withholds governed members during a
  count, grouping, or list reduction
- **THEN** the content-free outcome is recorded at that reduction without
  leaking the withheld members

#### Scenario: Nested alias emits only at the outer boundary

- **WHEN** `ask_memory` calls an internal search operation and then shapes its
  own final response
- **THEN** search contributes outcomes, the internal call emits nothing, and the
  successful outer boundary emits exactly one event

#### Scenario: External retry is a new disclosure attempt

- **WHEN** a caller retries a CLI, REST, or MCP read after an uncertain transport
  outcome
- **THEN** the new top-level invocation mints a new boundary id; Exomem does not
  collapse it by arguments or a possibly shared transport request id

#### Scenario: Internal anchor retry does not duplicate

- **WHEN** JSONL flush succeeds but observed-head update fails inside one
  invocation
- **THEN** the append helper retains the boundary id, recognizes that id at the
  verified tail, and repairs/adopts the anchor without appending a second event

#### Scenario: New mode cannot hide behind an existing command

- **WHEN** a content-returning mode is added to an existing product command
  without registering its reduction/projection adapter
- **THEN** registry-derived receipt coverage fails

### Requirement: Per-machine hash-chained log with truncation detection

Receipts SHALL be appended to a per-machine hash-chained log under
`_Governance/events/<instance-id>/`, each record linking to the previous by hash,
so a synced vault never forks a single chain. The sidecar SHALL distinguish the
last fsync'd durable head/sequence from the latest flushed observed
head/sequence. Month boundaries SHALL link across files.
Concurrent appenders sharing one instance id SHALL be serialized by a
process-safe receipt lock. The JSONL chain SHALL remain the evidence authority;
the sidecar SHALL be only an anchor and recovery index.
No append SHALL allocate a sequence until a bounded final-record read confirms
the actual tail matches an adopted observed head. Critical events SHALL fsync
JSONL before advancing the durable head. Reconcile SHALL verify and promote a
file-ahead suffix before later append; it MAY discard a power-loss-missing
observed suffix only when the actual file remains a valid extension of the
durable head. A tail behind or divergent from the durable head SHALL remain
blocked as truncation/tamper.

#### Scenario: Tamper is detected

- **WHEN** a record in the log is edited or the tail is truncated and the chain is
  verified
- **THEN** verification reports the break

#### Scenario: Sync does not fork the chain

- **WHEN** two machines each append receipts to a synced vault
- **THEN** each writes its own per-instance chain and neither corrupts the other's
  sequence

#### Scenario: Stale anchor cannot fork the chain

- **WHEN** a prior crash left the JSONL tail ahead of the sidecar anchor
- **THEN** the next append refuses pending verification/reconcile rather than
  linking a new record from the stale anchor

#### Scenario: Crash after JSONL fsync but before anchor update

- **WHEN** a critical record is durable in JSONL but the sidecar still names the
  preceding durable head
- **THEN** recovery verifies and promotes that suffix before allocating another
  sequence, without duplicating or forking the record

#### Scenario: Buffered suffix loss cannot roll back durable evidence

- **WHEN** power loss removes some non-fsynced read receipts after the observed
  head advanced
- **THEN** recovery may reset the observed cache only to a valid actual tail at
  or after the durable head, and never rolls the durable head backward

### Requirement: Critical events are receipt-first and crash-reconcilable

Before token consumption, governed deletion, or governed recovery changes state,
Exomem SHALL append and fsync a plaintext-free `intent` carrying a deterministic
event id and prior/target fingerprints. It SHALL append and fsync exactly one
terminal phase (`committed` or `aborted`) after the outcome is known. A restart
SHALL reconcile an unresolved intent idempotently from current state without
replaying the mutation. If current state matches neither prior nor target, the
event SHALL remain unresolved for manual review.

#### Scenario: Crash after state change retains durable evidence

- **WHEN** Exomem crashes after the state change but before appending the terminal
  phase
- **THEN** the fsync'd intent remains, reconcile appends `committed` only when
  current state matches the target, and the mutation is not replayed

#### Scenario: Ambiguous recovery does not guess

- **WHEN** an unresolved intent's current state matches neither its prior nor its
  target fingerprint
- **THEN** reconcile leaves it unresolved and reports that manual review is
  required

### Requirement: Deletion evidence outlives the source

Deleting a governed file or directory SHALL use the critical-event protocol and
carry affected refs, hashes, exact source/trash locations, and a batch manifest
without retaining deleted plaintext. After intent, Exomem SHALL fsync a
content-free tombstone before moving content. Every content-returning
operation/mode adapter and non-command download/frame/prompt/resource route SHALL
suppress tombstoned refs even when metadata, semantic, CLIP, or scene derivatives
remain. Coverage SHALL be structurally bound to the receipt-outcome branch
registry, with explicit non-command entries, and SHALL fail when a new branch
lacks tombstone gating.
Exact prior means captured source content present and exact trash target absent;
exact target means source absent, the captured trash manifest present, and zero
searchable derived residue after ordinary reconcile. Receipt reconciliation
SHALL run after ordinary derived-state reconciliation. It MAY repeat idempotent
derived cleanup but SHALL NOT repeat the semantic move.

Recovery from trash SHALL emit the inverse when the restored item is currently
governed or a matching governed deletion event exists, even if policy changed
after deletion. The exact restore and derived reindex SHALL remain hidden by the
tombstone until committed evidence is fsync'd; tombstone removal SHALL activate
recovery last. Exact recovery prior SHALL be captured trash present, source
absent, and deletion tombstone active. Exact staged target SHALL be source at the
captured hash, trash absent, required metadata/semantic/CLIP/scene derivatives
matched, and tombstone still active. Deletion receipts SHALL NOT be removed by
content deletion.

#### Scenario: Deletion leaves evidence, not plaintext

- **WHEN** a governed page or a directory containing governed pages is deleted
- **THEN** deletion receipts record the affected refs and hashes, and no deleted
  content is retained in the log

#### Scenario: Policy changes do not break recovery lineage

- **WHEN** a governed item was deleted and its governing policy is changed or
  removed before the item is recovered
- **THEN** the matching deletion event causes recovery to emit the inverse
  receipt and preserve the lineage

#### Scenario: Crash after move cannot terminal over searchable residue

- **WHEN** deletion moved the source but crashed before semantic/CLIP/scene
  cleanup or terminal evidence
- **THEN** the tombstone suppresses all stale derivatives, ordinary reconcile
  removes the residue first, and only then may receipt reconcile append committed

#### Scenario: Ambiguous placement stays hidden

- **WHEN** source/trash placement or hashes match neither exact prior nor target
- **THEN** reconcile retains the tombstone and blocks for manual repair

#### Scenario: Missing tombstone gate fails coverage

- **WHEN** a content-returning operation/mode branch or explicit non-command
  route is registered without tombstone suppression
- **THEN** the route-coverage gate fails before the surface can ship

### Requirement: Receipt state is not policy input

The reserved `events/**` and `deletion-tombstones/**` namespaces SHALL be
excluded from policy source discovery, policy signatures/fingerprints,
policy-cache dependencies, unknown-file findings, and generic policy
conflict-copy rejection. Receipt conflict copies SHALL instead fail receipt
append closed and be reported by receipt audit. The pending-policy marker SHALL
likewise not be compiled as policy, but the governance-tools loader SHALL check
it before policy compilation. Direct manual policy YAML behavior SHALL remain
unchanged.

#### Scenario: Receipt append does not churn policy cache

- **WHEN** a governed read appends an event or deletion writes a tombstone
- **THEN** warm-cache identity and the active policy fingerprint do not change,
  and cold policy load produces no unknown-file finding for that state

#### Scenario: Conflicted evidence does not disable existing enforcement

- **WHEN** sync creates a conflicted copy under a reserved receipt namespace
- **THEN** existing policy still compiles and enforces, while new governed egress
  fails closed at receipt append and audit reports the evidence conflict

### Requirement: Chain verification via audit

Chain verification SHALL be exposed as a read-only audit category reachable
through `maintain_memory(mode="audit")`, reporting edited records, truncated
tails, broken cross-month links, lagging anchors, and unresolved critical
intents without writing. The write-capable
`maintain_memory(mode="reconcile")` path MAY repair a lagging anchor only after
verifying the complete chain and MAY append an idempotent terminal only when
current state exactly matches the intent's prior or target fingerprint.

#### Scenario: Audit verifies the chain

- **WHEN** the governance-receipts audit category runs on an intact log
- **THEN** it reports the chain valid; on a tampered log it reports the break

#### Scenario: Audit reports but does not repair anchor lag

- **WHEN** the JSONL tail is intact and ahead of the sidecar anchor
- **THEN** audit reports the lag without changing either store, and an explicit
  reconcile may advance the anchor after full verification

#### Scenario: Reconcile dry-run is read-only

- **WHEN** `maintain_memory(mode="reconcile", dry_run=True)` finds an anchor or
  unresolved-intent repair
- **THEN** it reports the exact proposed repair without changing JSONL,
  tombstones, derived state, or sidecar

### Requirement: Governance sidecar migrations are monotonic

`governance/store.py` SHALL be the sole owner of the sidecar migration sequence.
The receipts foundation SHALL migrate schema v1 to v2; the dependent
governance-tools change SHALL migrate v2 to v3. Every sidecar opener SHALL
preserve a version newer than the schema it knows and SHALL NOT reset
`PRAGMA user_version`.

#### Scenario: Older opener preserves a later schema

- **WHEN** a token or policy reader from the receipts layer opens a v3 sidecar
- **THEN** it neither reruns v2 migrations nor lowers the stored version
