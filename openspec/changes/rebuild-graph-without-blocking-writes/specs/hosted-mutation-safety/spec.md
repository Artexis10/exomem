## MODIFIED Requirements

### Requirement: One Process-Safe Mutation Boundary Per Vault

The system SHALL serialize every canonical or reader-visible vault mutation
through one process-safe boundary keyed by canonical vault identity. MCP, REST,
CLI, transfer routes, and background writers MUST NOT maintain independent
locks for canonical Markdown, media, floor/checkpoint epoch state, live indexes,
logs, mutation receipts, or published runtime state. Synced vault files,
including hidden receipt artifacts, are untrusted/advisory for mutation
recovery; only owner-only local idempotency runtime state may authenticate a
receipt decision.

An epistemic-graph rebuild MAY build an unreachable private temporary sidecar,
and MAY perform scoped cleanup of a proven inactive reserved temporary sidecar,
outside the canonical boundary only under the dedicated rebuild lock defined by
`graph-rebuild-availability`. This exception SHALL NOT modify canonical files,
epoch state, receipts, logs, live graph state, or any reader-addressable path.
It SHALL never wait for the rebuild lock while holding canonical authority.

The private phase SHALL commit exact acknowledgement/checkpoint metadata,
truncate WAL, run full integrity and direct source/membership/topology proof,
and emit an immutable ticket binding exact graph publication epoch, direct
projection identity, warmed live `RecallFreshnessCheckpoint`, recall-policy
version plus bounded no-follow `_access.yaml` snapshot/fingerprint,
no-external-pending epoch, and exact closed temp-file/meta identity. Under
canonical authority, publication SHALL perform only two O(1)/bounded ticket
checks around the publication hook and then atomic replacement; it SHALL not
walk the vault or sidecar or rerun proofs. The rebuild lock is
cooperative builder/cleanup serialization; it is not authority to publish. A
POSIX runtime root used for that lock SHALL be owner-controlled and not
group/world-writable. Windows shall retain no-delete-share refusal for unsafe
live replacement.

#### Scenario: Private graph construction does not hold canonical authority

- **WHEN** a full graph rebuild fills a unique temporary database
- **THEN** only its rebuild lock covers vault-size-dependent construction
- **AND** unrelated canonical writers may enter the canonical boundary

#### Scenario: Publication retains canonical serialization

- **WHEN** a private graph build is ready to publish
- **THEN** it acquires canonical authority only for exact revalidation,
  acknowledgement, and atomic replacement
- **AND** it releases canonical authority before releasing the rebuild lock

#### Scenario: Publication authority contains no scalable work

- **WHEN** an immutable private publication ticket is ready
- **THEN** canonical authority performs only two bounded ticket checks around
  the publication hook and atomic replacement
- **AND** it performs no vault/sidecar walk, cache warm, WAL, integrity, or
  source/membership/topology proof

#### Scenario: External edits fail closed and converge

- **WHEN** an external Markdown or `_access.yaml` edit is observed during
  ticket validation
- **THEN** the epoch is marked pending and publication fails closed for
  reconcile/retry
- **AND** missed or unobserved direct edits are eventually detected by
  watcher-periodic reconcile
- **AND** arbitrary-editor linearizability is not claimed because it requires
  an out-of-scope broker/journal

#### Scenario: Concurrent commands from different product surfaces

- **WHEN** MCP and REST submit write-capable commands against the same vault at the same time
- **THEN** at most one command executes its mutation section at a time
- **AND** both commands reach the same existing command leaves after acquiring the shared boundary

#### Scenario: Separate processes target the same vault

- **WHEN** two Exomem processes resolve different path spellings to the same canonical vault and attempt mutations concurrently
- **THEN** they contend on the same process-safe vault boundary
- **AND** they cannot both enter their mutation sections

### Requirement: Mutation Boundary Composes With Receipted Transactional Writes

The boundary SHALL retain existing transactional rollback semantics for each
reader-visible canonical transition. For a claimed graph-relevant mutation, its
ordinary protocol is `floor → caller files → checkpoint → one atomically
installed authenticated GraphCommitReceipt v2 → local CAS`; there is no mutable
`commit_point` or two-write receipt marker. The receipt HMAC-SHA256 secret is a
fresh 32-byte per-attempt value retained only in the matching owner-only local
idempotency runtime state. A caught failure completes rollback while authority
remains held and does not install a receipt. Setup, reconcile repair, cleanup,
and rollback SHALL use `commit_point=False` and cannot manufacture a caller
commit; that API flag is not a stored receipt marker.

On Windows, that local state SHALL use only the bounded local SQLite binary
DPAPI `CurrentUser` BLOB and attempt/token-bound entropy defined by
`graph-rebuild-availability`, under its protected inheritable DACL for the
current service identity, `LocalSystem`, and `Administrators`. The hosted
service SHALL reject reparse-point or unsafe-DACL runtime paths before SQLite or
unpickle, plus truncated/trailing/unknown-version/provider BLOBs; account/profile
changes, copied BLOBs, failed unprotect, and raw legacy rows SHALL be
`outcome_unknown`, not recovery authority.

For signing, “complete ordinary protocol succeeded” SHALL mean only that the
claimed leaf returned a candidate terminal, the guarded vault batch successfully
applied floor/caller/checkpoint, and bounded canonical route-selection fanout
preconditions completed. It SHALL exclude receipt authentication/installation,
local lifecycle CAS, derived-handle registration, `ensure_started`, graph join,
and completed-terminal persistence. The signed terminal projection SHALL be the
closed path/content-free field set defined by `graph-rebuild-availability`; it
SHALL exclude `affected_paths`, every vault path/file name, and all arbitrary
leaf result or content.

Receipt v2 verification SHALL use the versioned HMAC input and closed canonical
JSON field set defined by `graph-rebuild-availability`, reject duplicate,
missing, surplus, malformed, or noncanonical fields before authentication, and
compare the recomputed HMAC in constant time. No boundary or hosted recovery
path may substitute a looser receipt parse.

The HMAC-covered v2 `canonical_disposition` SHALL be exactly `success` or
`committed_failure`. An authenticated `success` orphan may heal only its
matching trusted executing CAS. `committed_failure` suppresses leaf replay and
replays the retained exact local failure when available; otherwise it persists
`outcome_unknown` while graph recovery remains independent. Missing, invalid, or
fieldless v2 receipts never heal.

Full graph build/join begins only after the canonical boundary releases. Before
release, each checkpoint has an exact incremental acknowledgement, registered
rebuild handle, or durable failure handle; there is no accepted-but-unverified
state. The unavoidable cross-store cut before receipt v2 atomic installation
remains the fail-closed `outcome_unknown` terminal classification, never a
second leaf execution. A post-install CAS may heal only from that exact receipt,
matching trusted executing row, and retained secret. Dead reserved rows may be
reclaimed; dead executing rows without that evidence are outcome-unknown. That
classification is outside the four canonical lifecycle states and cannot be
treated as a committed receipt. A cleanup failure after commitment SHALL NOT
reconstruct success from an orphaned receipt.

#### Scenario: Multi-file mutation succeeds

- **WHEN** a governed write updates canonical files and requires graph recovery
- **THEN** it commits one exact receipt only after the full canonical protocol
- **AND** any graph wait occurs after the boundary releases

#### Scenario: Transactional batch rolls back

- **WHEN** a caught failure occurs before receipt v2 atomically installs
- **THEN** the boundary completes rollback before another canonical mutation can observe partial state
- **AND** the retry row cannot replay a committed terminal

#### Scenario: Canonical receipt cut is not inferred

- **WHEN** a process dies after a canonical file can have committed but before receipt v2 atomically installs
- **THEN** retry returns outcome-unknown/readback guidance
- **AND** no product surface re-executes the canonical leaf

#### Scenario: Copied receipt is not cross-replica authority

- **WHEN** a replica sees a copied synced receipt but lacks its matching local row and secret
- **THEN** it treats the receipt as advisory and does not suppress a mutation
- **AND** cross-replica exactly-once remains deferred until a shared pre-leaf CAS exists
