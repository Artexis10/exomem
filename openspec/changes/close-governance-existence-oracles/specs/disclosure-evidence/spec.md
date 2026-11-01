## MODIFIED Requirements

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

A governed boundary that collects no outcome SHALL still emit one receipt with an empty outcome list when the caller is restricted or the compiled policy is non-empty, so that a receipt-store failure refuses it exactly as it refuses a boundary that touched an item. The owner on a vault with an empty policy SHALL keep the receipt-free fast path.

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

#### Scenario: A probe that touches nothing is receipted

- **WHEN** a restricted caller reads a missing path or runs a search with no hits
- **THEN** the boundary emits one disclosure receipt with an empty outcome list
- **AND** when the receipt store is unavailable, the call refuses with the same retryable error as a call that touched an item

#### Scenario: The ungoverned owner fast path stays receipt-free

- **WHEN** the owner reads a missing path on a vault with an empty policy
- **THEN** no receipt is appended

### Requirement: Chain verification via audit

Chain verification SHALL be exposed as a read-only audit category reachable
through `maintain_memory(mode="audit")`, reporting edited records, truncated
tails, broken cross-month links, lagging anchors, and unresolved critical
intents without writing. The write-capable
`maintain_memory(mode="reconcile")` path MAY repair a lagging anchor only after
verifying the complete chain and MAY append an idempotent terminal only when
current state exactly matches the intent's prior or target fingerprint.

Verification SHALL report, for each instance, whether a local anchor covers it, and SHALL count the unanchored instances. An unanchored instance SHALL NOT make the chain invalid, because each machine anchors only its own instance and an imported vault starts without anchors. The audit category SHALL report each unanchored instance as a warning, never as a verified pass.

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

#### Scenario: A copied chain reads as unanchored, not verified

- **WHEN** chain verification runs on a vault whose instance has records but no local anchor
- **THEN** it reports that instance with `anchored: false` and counts it in `unanchored_instances`
- **AND** the audit category reports a warning for it
