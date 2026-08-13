## MODIFIED Requirements

### Requirement: Canonical policy source in the vault

Governance policy SHALL be authored as strict YAML documents under
`Knowledge Base/_Governance/` — `scopes/*.yaml`, `rules/*.yaml`, `grants/*.yaml` —
one document per file, each carrying an immutable ULID `id` and
`governance_version: 1`. These documents SHALL remain the canonical reviewable
authoring workspace and history, but mutable workspace bytes SHALL be pending source
input and MUST NOT become active enforcement authority merely because they were
created, edited, renamed, conflicted, or deleted.

Runtime enforcement authority SHALL be exactly one `active_governance_tuple` in the
private no-follow `Knowledge Base/.governance.sqlite` activation store. Its value SHALL
be exactly `(policy_generation_id, policy_fingerprint, projector_schema_version,
catalog_generation)`, and no separately active policy pointer, catalog pointer,
projector selector, raw index, graph, or cache may override it. Every policy generation
SHALL retain the exact ordered source-document bytes and source fingerprint from which
its canonical compiled bytes and fingerprint were derived; every catalog generation and
projection namespace it names SHALL be immutable and independently verifiable.
Historical generations SHALL be append-only.

The protected external cell registry SHALL carry a monotonic `governance_enrolled` flag
and, after irreversible enrollment, the expected
`(activation_store_id, activation_epoch, activation_state_digest)`. Serving SHALL require
exact registry/store/active-tuple parity. `activation_state_digest` SHALL be
`SHA-256("exomem.activation-state.v1\\0" || JCS(value))`, where RFC 8785 `value` contains
exactly logical-vault id, immutable activation-store id, monotonic activation epoch,
policy generation id/fingerprint/immutable-row digest, projector schema version, catalog
generation/immutable-descriptor digest, and projection-namespace identity.
`_Governance/`, the activation store and its
WAL/SHM/journal family, and every registered internal raw/projected index/catalog file
SHALL be excluded structurally from content membership and SHALL never be returned as
knowledge, including to owner/L6 or through non-Markdown classification.

#### Scenario: Policy files are not indexed as content

- **WHEN** a `find`/`ask_memory` query runs on a vault with `_Governance/` policy files
- **THEN** no workspace policy file, compiled generation, or activation-store field
  appears as a hit

#### Scenario: Unknown workspace version remains pending and fails closed

- **WHEN** a workspace policy file declares an unrecognized `governance_version` or
  carries an unknown field
- **THEN** prospective compilation refuses with a clear owner finding, the immutable
  active tuple is not changed, ordinary content serving fails closed until workspace
  repair, and the invalid bytes do not become authority

#### Scenario: Direct workspace edit is not publication

- **WHEN** an external editor changes a valid `_Governance` document without a reviewed
  `govern_memory` commit
- **THEN** the active tuple and effective policy/catalog remain unchanged and owner
  diagnostics report the valid workspace as pending next-generation input

#### Scenario: Corrupt activation authority does not fall back to workspace

- **WHEN** the external enrollment record, activation store identity/epoch/digest,
  active tuple, named policy/catalog row, projection namespace, schema version, or
  no-follow store identity cannot be verified
- **THEN** enforcement fails closed and neither mutable workspace bytes nor another
  historical generation/catalog or cached last-good/OPEN object is selected implicitly

#### Scenario: Enrolled workspace deletion stops serving

- **WHEN** `governance_enrolled=true` and `_Governance` is deleted, unreadable,
  conflicted, symlinked, or unparsable while the process is stopped or running
- **THEN** restart and warm-process reads both fail closed; the active tuple remains
  immutable but is not served until owner repair

### Requirement: Fingerprinted compile with empty-policy fast path

The kernel SHALL distinguish a workspace source fingerprint from the active compiled-
policy fingerprint. A prospective source fingerprint SHALL change whenever any policy
file byte changes, including timestamp-preserving replacement. The active policy
fingerprint SHALL change only when one complete immutable generation is atomically
selected in the complete active tuple. Only a fresh authenticated external-registry
record with `governance_enrolled=false`, null expected activation tuple, and no
`_Governance` or registered internal activation artifact MAY return the cached OPEN
singleton with stable `missing` fingerprint. Absence of files alone is never proof.
Once enrollment begins it is irreversible: a reviewed empty policy is an enrolled
governed generation, and missing/corrupt workspace/store/tuple state is BLOCKED rather
than OPEN.

Every user-visible prospective compile SHALL bind one stable live authoring snapshot.
It SHALL capture the exact workspace byte map and source fingerprint, conflict-set
digest, pending-authoring guard generation, no-follow regular-file identities, current
active policy/projector/catalog tuple, prospective canonical compiled bytes and target
fingerprint, and affected membership manifest. Conflict and identity probes SHALL run
before and after reading; an unstable, conflicted, aliased, symlinked, or non-regular
workspace SHALL return no reviewable target. A pure document compiler MAY be used
without live probes only for bytes already pinned by immutable generation, proposal,
journal, or receipt evidence.

Commit SHALL revalidate the proposal's complete expected active tuple and affected
membership, build the exact target-policy projection namespace for the expected catalog,
then use one SQLite `BEGIN IMMEDIATE` transaction to insert the complete append-only
target generation and compare-and-swap `active_governance_tuple` to `(target policy
generation/fingerprint, target projector version, expected catalog generation)`. Tuple,
receipt activation state, store-side activation epoch/digest, and ready namespace
reference SHALL commit atomically. The protected external registry SHALL then
compare-and-swap its expected tuple to those exact committed values; serving during any
mismatch SHALL be BLOCKED, and recovery may acknowledge only the receipt-proven committed
tuple. Active readers SHALL snapshot the tuple once and use only its
policy generation, immutable catalog, and exact projection namespace for the whole
request. The committed tuple transaction SHALL be the authority-publication
linearization point; a pre-write filesystem comparison, workspace mirror, independently
mutable catalog pointer, or cache SHALL NOT be treated as activation.

Every content create/edit/delete and companion mutation SHALL stage a complete immutable
next catalog plus all required raw/projected lanes for the expected policy/projector,
then CAS the same active tuple from that expected policy to the next catalog generation.
Policy commits SHALL CAS the catalog they reviewed; content/companion commits SHALL CAS
the policy they reviewed. A race SHALL have one tuple winner and one stale refusal/retry,
never an active policy/catalog pairing lacking its exact namespace. A held artifact or
companion identity/hash that differs from its active catalog row before publication SHALL
fail content-free as stale/warming and MUST NOT be served through the prior projection.

Writing the reviewed documents back to `_Governance` SHALL be a separate mirror under a
cooperative writer fence, with held-parent no-follow traversal and descriptor-identity
checks against the exact captured workspace identities. An observed byte or identity
drift SHALL refuse the mirror and leave those bytes pending next-generation input with
owner-only conflict/parity diagnostics; they SHALL NOT alter the immutable reviewed
generation or become active. Direct OS-owner mutation is outside the cooperative writer
fence. Mirror failure
alone need not abort an otherwise exact tuple transaction. Recovery SHALL derive source/
generation parity from the immutable source byte map, never from a fresh workspace walk,
and SHALL fail closed rather than select mutable or historical bytes when the active tuple
or external expected activation digest cannot be verified.

#### Scenario: Empty policy short-circuits

- **WHEN** the protected external registry proves `governance_enrolled=false` with a
  null expected activation tuple and no `_Governance` or internal activation artifact
- **THEN** it returns the OPEN singleton without opening the governance sidecar,
  and any decision resolves to full disclosure (L6)

#### Scenario: Missing enrollment proof is not empty policy

- **WHEN** the external registry is missing, stale, corrupt, unreachable, says enrolled,
  or contradicts on-disk workspace/store state
- **THEN** the kernel returns BLOCKED and never enters the OPEN singleton

#### Scenario: Timestamp-preserving edit still invalidates

- **WHEN** a policy file's content changes but its mtime is preserved
- **THEN** the next prospective workspace source fingerprint changes while the active
  generation and effective policy remain unchanged until reviewed publication

#### Scenario: Conflicted copy refuses compile

- **WHEN** a supported conflict-copy policy sibling is present
- **THEN** prospective compile refuses, the active generation remains in effect, and the
  workspace conflict is reported for resolution

#### Scenario: Prospective compile refuses a live conflict

- **WHEN** a conflict copy exists before prospective policy documents are overlaid
- **THEN** prospective compilation refuses with the same conflict identity commit would
  validate and returns no reviewable target

#### Scenario: Concurrent live change invalidates prospective acquisition

- **WHEN** live policy bytes, the conflict set, or the pending authoring guard changes
  between the prospective compiler's before and after probes
- **THEN** the compile refuses as unstable and does not return a mixed snapshot

#### Scenario: Commit is bound to reviewed base and target

- **WHEN** any active tuple component, affected membership, stored target bytes, target
  fingerprint, or required projection-namespace readiness differs from the proposal at
  the tuple transaction
- **THEN** compare-and-swap refuses as stale, selects no target, and requires a fresh
  proposal unless receipt recovery already proves that exact transaction committed

#### Scenario: Observed workspace drift remains pending

- **WHEN** held-parent/descriptor checks observe a reviewed policy target changed after
  the last preflight probe but before or after the active-tuple transaction
- **THEN** the mirror refuses, the exact reviewed generation may activate if its active-
  base and membership checks still pass, and the observed bytes remain pending with a
  source/generation divergence diagnostic. Direct OS-owner mutation outside cooperative
  fencing is outside the cooperative writer fence

#### Scenario: Competing policy publication has one winner

- **WHEN** two reviewed commits compare-and-swap from the same active tuple
- **THEN** one tuple transaction may commit and the other refuses stale; no reader sees
  a hybrid generation and neither commit overwrites the other's immutable row

#### Scenario: Active tuple transaction is the concurrency cut

- **WHEN** the complete immutable target, receipt activation state, and ready projection
  namespace commit with the active-tuple compare-and-swap
- **THEN** that SQLite transaction is the publication linearization point and every
  request observes either the complete predecessor tuple or complete target tuple

#### Scenario: Policy and content publication race has one tuple winner

- **WHEN** a policy commit expecting catalog C races a content create, edit, delete, or
  companion commit expecting policy P from the same active tuple
- **THEN** exactly one complete tuple CAS commits; the loser refuses/rebuilds against the
  winner, and no reader sees P-next with an old projection or C-next under an old policy

#### Scenario: Reader never joins independently sampled generations

- **WHEN** policy, projector, or catalog publication occurs during a retrieval request
- **THEN** the request uses one previously snapped active tuple and never combines a
  newly sampled policy, graph, raw index, projected index, or catalog with it

#### Scenario: Recovery compiles only pinned bytes

- **WHEN** recovery invokes the pure document compiler
- **THEN** every input byte and target fingerprint comes from the operation's durable
  immutable generation/journal/receipt evidence rather than a new unguarded workspace
  walk

#### Scenario: Direct-source migration initializes one authority

- **WHEN** a v3 vault with valid live YAML is migrated under the cooperative whole-tree
  writer/schema fence
- **THEN** migration first marks external enrollment irreversible, snapshots and
  rechecks those bytes, stores one immutable compiled generation plus catalog descriptor,
  initializes its active tuple atomically, records the exact expected activation
  id/epoch/digest, and serves no v4 request before all authority verifies

### Requirement: Query-time scope membership

Scope membership SHALL be evaluated at query time against an already-parsed Markdown
page or an explicit non-Markdown classification outcome using the scope's selectors
(path globs, projects, tags, types, detector classes, explicit refs) minus its `exclude`
selectors, memoized per immutable content identity and policy fingerprint. Membership
SHALL NOT be materialized as an index-time table and SHALL NOT add a component to the
deletion/upsert fan-out. A policy change SHALL invalidate membership by fingerprint
mismatch.

Non-Markdown membership SHALL distinguish `CLASSIFIED(scope_ids)` from `UNRESOLVED`.
For each scope, a path/ref exclusion SHALL first prove exclusion and a path/ref positive
SHALL next prove membership without semantic metadata. Only a still-undecided scope
whose positive selectors include project, tag, type, or class requires a valid companion.
If that companion is missing, malformed, unreadable, stale, or unsafe, the entire
artifact classification SHALL be `UNRESOLVED`. Missing companion metadata MUST NOT be
interpreted as empty metadata or an empty scope set. Every release, structured-read,
proposal, and grant-drift caller SHALL translate `UNRESOLVED` to the fail-closed
L0/missing contract. A policy containing only path/ref selectors remains classifiable
without a companion.

Semantic companions SHALL be located and bound by a closed artifact-class registry:
ordinary/media binaries use the sibling `<artifact-leaf>.md`; datasets use the unique
canonical `type: dataset` card whose normalized `data_file` equals the artifact path; and a
scene frame uses its sibling `<frame-leaf>.md` inside the canonical
`<parent-media>.frames/` directory. Every valid companion SHALL carry a versioned
`governance_companion` descriptor with `state: classified`. Its immutable binding tuple
SHALL contain class, normalized artifact path, artifact SHA-256, and byte size; media also
contains media type and original filename; dataset also contains declared format; and a
scene frame also contains parent path/hash and an integer frame timestamp that agrees with
the canonical filename. That field SHALL be named `frame_timestamp_ms`, have integer
range `0..4_294_967_295`, and equal the integer milliseconds encoded in
`scene-<NNN>-t<frame_timestamp_ms>ms.jpg`. The descriptor SHALL also contain an exact `semantics` mapping
whose only keys are canonical string lists `projects`, `tags`, `types`, and `classes`;
these values classify the artifact and are distinct from metadata classifying the
companion page. The artifact and companion SHALL be canonical regular files read
from immutable snapshots. Zero/multiple dataset cards, a missing/incomplete descriptor,
binding mismatch, unsafe locator, changed bytes, or frame-parent/timestamp mismatch SHALL
be `UNRESOLVED` for every still-undecided semantic scope.

A descriptor with `state: classified`, a valid binding tuple, and all four semantic lists
deliberately empty SHALL be the only valid explicitly empty semantic companion.
Missing semantic keys without that marker MUST NOT mean empty. Pre-descriptor legacy media
sidecars—including the minimal stubs emitted by `preserve.py`, pending worker sidecars, and
completed transcripts—SHALL remain classifiable by path/ref policy only.

Backfill SHALL require an owner-authorized, receipt-first
`govern_memory(operation="backfill_companion")` proposal/commit. Version-1 input SHALL
contain exactly the artifact class, canonical artifact path, expected artifact SHA-256
and size, expected companion path and SHA-256, complete explicit four-list `semantics`,
and every class-specific binding field. The trusted adapter SHALL establish the local
owner. Companion-page tags/projects/type/classes, artifact metadata, policy, model text,
dataset rows, and media jobs MUST NOT authorize backfill or be inferred into artifact
semantics. Preview SHALL return the exact target descriptor and immutable identities;
commit SHALL revalidate them under the no-follow mutation boundary, write only the
descriptor, and record owner, proposal, prior/target hashes, and terminal outcome. It
SHALL preserve transcript/body, page-level metadata, and processing state, infer neither
non-empty nor empty semantics, remain idempotent only for the same committed input, and
refuse without partial metadata on ambiguity or drift.

Legacy scene-frame conversion SHALL parse finite non-negative `frame_ts`, calculate
`int(round(binary64(frame_ts) * 1000))` using round-to-nearest-ties-to-even, require the
bounded result to equal both the filename milliseconds and indexed frame timestamp, and
then store `frame_timestamp_ms`. A negative, non-finite, out-of-range, ambiguously
indexed, or disagreeing value SHALL refuse. After migration, the float SHALL NOT be
membership authority. New companions SHALL carry the descriptor at creation.

#### Scenario: Selector kinds resolve membership

- **WHEN** a page matches a scope by any selector kind and is not caught by an
  `exclude` selector
- **THEN** the page is a member of that scope

#### Scenario: Policy change invalidates the memo

- **WHEN** the policy fingerprint changes
- **THEN** previously memoized membership is recomputed against the new policy

#### Scenario: Missing companion under semantic-only scope is unresolved

- **WHEN** a non-Markdown artifact has no companion Markdown and a non-excluded scope
  can select it only by project, tag, type, or class
- **THEN** membership is `UNRESOLVED`, every non-owning content/structured surface treats
  the artifact as L0/missing, and no caller receives an empty-scope open decision

#### Scenario: Malformed companion is also unresolved

- **WHEN** the required companion exists but cannot be parsed, read consistently, or
  tied to the artifact's immutable identity
- **THEN** membership fails closed identically to a missing companion

#### Scenario: Path-only policy needs no companion

- **WHEN** every scope uses only path/ref selectors and exclusions and a non-Markdown
  artifact matches none of them
- **THEN** membership is the classified empty set and the existing unmatched default
  applies

#### Scenario: Path exclusion proves semantic scope exclusion

- **WHEN** a non-Markdown artifact matches a scope's path/ref exclusion before semantic
  selectors are needed
- **THEN** that scope is proven excluded without requiring companion metadata

#### Scenario: A path match does not erase an unresolved sibling scope

- **WHEN** a path/ref selector proves membership in scope A while semantic-only scope B
  cannot be evaluated because companion metadata is missing
- **THEN** the artifact remains `UNRESOLVED` and a grant for A cannot make it public

#### Scenario: Explicitly empty companion is classified only when bound

- **WHEN** a non-Markdown artifact's canonical companion carries `state: classified`, no
  semantic selector values, and the complete tuple matches its immutable artifact snapshot
- **THEN** the semantic result is a valid explicit empty classification rather than
  unresolved

#### Scenario: Legacy preserve stub needs verified backfill

- **WHEN** a `preserve.py` legacy media stub or pending/completed sidecar lacks the complete
  descriptor under an applicable semantic-only scope
- **THEN** the artifact is unresolved until backfill verifies and binds the current binary,
  and backfill preserves the existing body and processing state

#### Scenario: Backfill semantics require explicit owner input

- **WHEN** a legacy companion page has tags/projects/type/classes but the reviewed
  backfill input omits or differs from its explicit artifact `semantics`
- **THEN** Exomem does not copy page metadata, refuses incomplete input, and writes no
  descriptor without an authenticated owner proposal and receipt-backed commit

#### Scenario: Backfill race is receipt-first and atomic

- **WHEN** artifact, companion, parent, dataset card, or expected bytes drift after
  preview or the operation crashes at any receipt/mutation boundary
- **THEN** state is exact prior or the one reviewed descriptor with a recoverable
  terminal; no partial tuple or inferred semantics appears

#### Scenario: Dataset card is unique and content-bound

- **WHEN** no card, two cards, or a stale card points at the same dataset, or its declared
  format/hash/size does not match the immutable data snapshot
- **THEN** dataset membership is unresolved and no row, aggregate, profile, count, or parse
  diagnostic crosses the boundary

#### Scenario: Scene frame binds its parent and timestamp

- **WHEN** a scene-frame companion's parent hash/path or timestamp differs from the frame
  directory, canonical filename, or current parent snapshot
- **THEN** both semantic membership and the frame-to-parent recall mapping are unresolved

#### Scenario: Legacy frame timestamp migration is canonical

- **WHEN** a legacy finite `frame_ts` rounds ties-to-even to the same bounded integer
  milliseconds encoded in the canonical filename and index
- **THEN** owner-reviewed backfill stores that exact `frame_timestamp_ms`; any rounding,
  range, filename, parent, or index mismatch refuses without choosing a source

### Requirement: Pure order-free disclosure evaluator

The kernel SHALL expose a pure function mapping `(item scope_ids, audience, purpose,
active grants)` to a disclosure ceiling. It SHALL evaluate each matched scope
independently. For one scope, the standing value is the minimum matching standing rule,
defaulting to L0 when that scope declares default-deny for a non-owner and to L6
otherwise; only grants explicitly bound to that scope may raise that value; matching
organisation caps may then lower it. The item ceiling SHALL be the minimum of those
per-scope ceilings, defaulting to L6 when the item is classified into no scope.

The function SHALL be free of IO beyond its compiled-policy and grant inputs, SHALL be
order-independent (no rule priority), and SHALL treat an undeclared purpose
deterministically: a purpose-conditioned allowance does not apply, while an "outside
purpose" restriction does. A declared purpose MUST NOT produce a result more permissive
than the undeclared result. Active session grants SHALL carry exact scope IDs from their
reviewed membership manifest and policy fingerprint; an unscoped legacy session grant
SHALL NOT participate.

Decision options SHALL compose conservatively per scope rather than overwrite by rule-id
order. The compiler SHALL accept only a closed, versioned option registry. Authored rule
options are exactly bounded provenance-free strings `notice`, `constraint`, `abstract`,
and `bridge`, plus boolean control `suspended`, which is applied before matching and
never enters a decision.
Release-grant options are exactly non-empty canonical `strip_provenance` ref/path lists.
`constraint_source` and `constraint_ambiguous` are derived fields and SHALL NOT be
authorable. There is no authored numeric option in this version. An unknown key, wrong
type, unsafe/oversized value, non-canonical ref/path, custom YAML container/tag, or
non-finite value SHALL be a compile ERROR; it SHALL NOT survive opaquely in `Rule.options`.
`credential_scrubber` is retired and absent from the registry. Every authored spelling
or value, including legacy `false`/`off` and `true`/`on`, SHALL be that same compile ERROR
and require owner-reviewed removal; it SHALL NOT influence a decision or disable the
non-optional terminal scrubber.

Every registered option SHALL have a canonical associative, commutative, and idempotent
meet. Restrictive strip sets SHALL be normalized unions. Content-bearing singletons and
derived release identities/digests SHALL survive only when every contributing tied
operand has the same canonical value; absence or disagreement cannot be filled by a
sibling. Ambiguity SHALL lower below the level that would emit it, and conflicting L1
notices SHALL lower to L0.
The same decision meet SHALL fold tied rules inside a scope, matched scopes, and complete
declared/undeclared purpose decisions. Before the purpose meet, both branches SHALL be
projected to their lower numeric level; the result SHALL be no more informative than
either branch. Equal numeric purpose ceilings MUST NOT select the declared branch or add
any field. A scope grant SHALL change only that scope's ceiling, carry no options, and
SHALL NOT import options from another scope.

#### Scenario: Most restrictive value wins inside one scope

- **WHEN** multiple standing rules, a scope-bound grant, and an organisation rule all
  match one scope
- **THEN** that scope's ceiling is the organisation cap over the grant over the minimum
  standing value, independent of authoring order

#### Scenario: Item ceiling is the minimum across scopes

- **WHEN** an item belongs to scopes A and B whose independently evaluated ceilings are
  L5 and L0
- **THEN** the item ceiling is L0 regardless of document or scope ordering

#### Scenario: Grant cannot cross an overlapping scope

- **WHEN** an item belongs to default-deny scopes A and B and a grant explicitly names
  only A
- **THEN** A may be raised within the grant bounds, B remains L0, and the item remains L0

#### Scenario: Grant explicitly covering every scope can lift each one

- **WHEN** an item's active grant is bound to every matched scope and no organisation cap
  lowers the result
- **THEN** each scope is raised only to its own grant ceiling and the item receives the
  minimum raised value

#### Scenario: Default is full disclosure

- **WHEN** an item is classified into no scope for a given audience
- **THEN** the ceiling is full (L6)

#### Scenario: Undeclared purpose is deterministic

- **WHEN** a rule allows an item only for purpose P and no purpose is declared
- **THEN** the allowance does not fire; and an "outside P" restriction does fire

#### Scenario: Equal-level purpose cannot add an option

- **WHEN** the declared and undeclared purpose branches both resolve to L3 but only the
  declared branch supplies an abstract, notice, bridge, or release identity
- **THEN** the final decision retains none of those additional fields and is no more
  informative than either branch

#### Scenario: Session grant without scope identity is refused

- **WHEN** a legacy active session-grant row contains item paths but no exact reviewed
  scope binding
- **THEN** it contributes no ceiling and the caller must obtain a fresh authorization

#### Scenario: Tied scope constraints do not use last-write-wins

- **WHEN** two matched scopes both resolve to L2 but provide different canonical
  constraints
- **THEN** the item is lowered to L1, neither constraint is emitted, and reversing rule
  IDs or document order changes nothing

#### Scenario: Conflicting notices fail below notice

- **WHEN** two matched scopes tie at L1 with different notice text
- **THEN** the item resolves to L0 rather than selecting either notice

#### Scenario: Restrictive and permissive options compose conservatively

- **WHEN** overlapping operands contribute different strip sets or content singletons
- **THEN** strips are unioned and singleton absence/disagreement cannot add content

#### Scenario: Legacy scrubber option refuses compile

- **WHEN** any rule in a candidate workspace carries `credential_scrubber`, including
  legacy on/off or boolean syntax
- **THEN** prospective compilation returns an ERROR, the active compiled generation stays
  selected, and the owner must review removal of the field

#### Scenario: A raised sibling cannot import options

- **WHEN** a grant raises scope A to a level with a bridge while overlapping scope B
  remains lower or supplies a conflicting bridge
- **THEN** A's grant does not overwrite B's options and the final item projection uses
  the conservative item-level result

#### Scenario: Option meet is independent of order and grouping

- **WHEN** the same tied rules, scopes, and purpose branches are folded through every
  permutation and parenthesization, including absent/conflicting singleton values
- **THEN** every result has the same canonical level/options and satisfies commutativity,
  associativity, idempotence, and information no greater than every operand

#### Scenario: Unknown or untyped option refuses compilation

- **WHEN** a rule authors an unregistered option, a compiler-owned derived key, a custom
  YAML container/tag, an invalid boolean spelling, or an unsafe content string
- **THEN** compilation emits an ERROR and no decision can preserve or order-merge the value

### Requirement: Guarded Fallback Never Serves An Open Policy

OPEN SHALL be returned only from a fresh authenticated external cell-registry record
that proves `governance_enrolled=false`, carries a null expected activation tuple, and
agrees with absence of `_Governance` and every registered activation-store artifact.
File absence, a cached empty compile, a process-local last-good value, or an old
`missing` fingerprint SHALL NOT establish never-governed state.

The first governance enrollment SHALL durably and irreversibly set
`governance_enrolled=true` before creating workspace/store state. Thereafter every load
SHALL verify the expected activation store id, monotonic epoch, activation digest, active
policy/projector/catalog tuple, immutable policy/catalog rows, ready projection namespace,
and valid workspace. A valid pending workspace edit or authoring guard may continue to
serve that verified tuple, but missing/corrupt/conflicted workspace or authority state
SHALL return BLOCKED. It MUST NOT serve OPEN, a historical tuple, mutable YAML, or an
in-process last-good fallback.

#### Scenario: First enrollment crash cannot reopen

- **WHEN** irreversible external enrollment commits and the process crashes before the
  workspace, activation store, generation, or active tuple is complete
- **THEN** warm and cold readers return BLOCKED and never the OPEN singleton

#### Scenario: Cached open does not survive enrollment

- **WHEN** one process cached OPEN before external `governance_enrolled` became true
- **THEN** its next content request revalidates the external record and refuses OPEN even
  if all enrolled on-disk state was deleted while the process was stopped

#### Scenario: Valid pending source keeps one verified authority

- **WHEN** an enrolled vault has a syntactically valid direct workspace edit or an
  authoring guard pending while registry/store/tuple parity remains exact
- **THEN** readers use the single verified active tuple; the pending source neither
  activates nor causes a historical/OPEN fallback

#### Scenario: Missing or corrupt enrolled state is always blocked

- **WHEN** the workspace, activation store, active tuple, named policy/catalog generation,
  projection namespace, or external expected identity/epoch/digest is deleted, corrupted,
  aliased, stale, or unavailable before restart or during a warm process
- **THEN** every content-returning surface fails closed with the content-safe BLOCKED
  floor until explicit owner repair restores exact parity

#### Scenario: Warm and cold processes agree

- **WHEN** the same enrolled fault or in-progress activation is observed by a process
  that previously served OPEN/another tuple and by a fresh process
- **THEN** both derive authority from the same external record plus active tuple and make
  the same non-OPEN disclosure decision

### Requirement: Sync Conflict Copies Refuse Policy Compile

Mutable `_Governance` documents SHALL be pending authoring input, not policy authority.
Conflict-copy detection SHALL nevertheless recognize every supported synchronizer form,
including the parenthesized Obsidian marker and hyphenated sync-conflict marker, across
prospective policy discovery, held-handle workspace walks, mirrors, and the receipt tree.
A recognized workspace conflict SHALL make the enrolled workspace invalid: prospective
compile and every authoring operation refuse, and ordinary content serving fails closed
until repair. It SHALL NOT select either copy, synthesize a document set, or fall back to
OPEN, mutable YAML, or a historical policy.

Creating, editing, or deleting an ordinary conflict-free YAML document directly SHALL
change pending source only. In particular, deleting a grant document does not revoke
the grant in the active tuple. Revocation becomes effective only when a reviewed commit
stores the exact resulting immutable generation and atomically publishes it in the
complete active tuple. A conflict copy of a deleted or retained grant cannot restore,
remove, or otherwise change active authority.

#### Scenario: Direct deletion is pending rather than revocation

- **WHEN** an external editor deletes an active grant's conflict-free workspace document
- **THEN** the active tuple and grant remain unchanged, diagnostics show pending deletion,
  and revocation requires a reviewed commit of the new immutable generation

#### Scenario: Conflict copy of deleted grant changes no authority

- **WHEN** a synchronizer lands a conflict copy of a deleted grant
- **THEN** prospective compile and content serving fail closed, the active tuple is not
  changed, and the conflict copy neither restores nor revokes the grant

#### Scenario: Original plus conflict refuses before duplicate-id compile

- **WHEN** an ordinary policy document and its recognized conflict copy are both present
- **THEN** acquisition refuses at conflict detection before duplicate-id compilation and
  no reviewable target is returned

#### Scenario: Receipt-tree conflict cannot fork evidence

- **WHEN** a recognized conflict copy appears inside the per-machine receipt tree
- **THEN** receipt append and dependent authoring fail closed and the hash chain is not
  extended from either conflicted record

#### Scenario: Ordinary similarly named document is only pending input

- **WHEN** a document contains neither registered conflict marker and the workspace is
  otherwise valid
- **THEN** it participates in a stable prospective snapshot without becoming active
  until the reviewed active-tuple transaction commits

### Requirement: A Conflict Copy Refuses Policy Authoring While Reads Continue

Every governance-authoring operation SHALL refuse while a recognized conflict copy is
present under the governance workspace or receipt tree. The refusal SHALL occur before
proposal reservation, generation insertion, mirror, journal/marker allocation, receipt
append, or active-tuple mutation; pre-existing activation-store state SHALL remain
byte-for-byte unchanged.

"Reads continue" SHALL mean the service still returns the registered content-safe
fail-closed response and owner-only conflict diagnosis rather than crashing or guessing.
Because an enrolled conflicted workspace is corrupt trusted input, ordinary content
reads MUST NOT serve OPEN or the active generation until the conflict is resolved. Once
resolved, the existing active tuple may serve again if every external/store/workspace
integrity check passes; the newly resolved workspace remains pending and cannot activate
without a reviewed commit.

#### Scenario: Authoring refusal creates no new state

- **WHEN** a conflict copy is present and any governance-authoring operation is invoked
- **THEN** it returns the distinct content-safe conflict code and creates no proposal,
  generation, receipt, marker, mirror, or tuple update

#### Scenario: Content reads fail closed without guessing

- **WHEN** the same enrolled vault receives a content read while its workspace conflict
  exists
- **THEN** the response is the common BLOCKED/missing floor, not OPEN, a selected conflict
  branch, mutable YAML, or an unverified last-good object

#### Scenario: Resolving conflict restores review but not publication

- **WHEN** every conflict copy is removed and workspace/store/external parity verifies
- **THEN** the existing active tuple may serve and authoring may propose again, but the
  resolved bytes become authority only after a reviewed tuple commit

### Requirement: A Scope May Deny Audiences It Does Not Name

A scope MAY declare that the standing default for an audience is the most restrictive
level rather than the most permissive one. Where a scope carries that declaration and an
item is a member of it, an audience for which no standing rule matches that scope SHALL
resolve to no disclosure for that scope, instead of to full release.

The declaration changes the DEFAULT only. It SHALL NOT override an authored rule: where
a standing rule names the audience for that scope, that rule's ceiling applies exactly
as it does today. Grants SHALL continue to only raise the scopes they explicitly name,
organisation caps SHALL continue to only lower, and a declared purpose SHALL continue
to only narrow.

The owner SHALL never be subject to the declaration. An owner locked out of their own
scope is a vault that has lost its own contents, which is not the confidentiality this
expresses. Authored rules and organisation caps that explicitly name the owner remain
effective.

Where an item is a member of several scopes, each scope SHALL choose its own standing
default and grant contribution, and the most restrictive per-scope result SHALL govern
the item. A declared scope cannot be widened by adding an undeclared scope or by granting
a different overlapping scope.

A vault SHALL remain on the empty fast path only when the protected external registry
proves it has never been enrolled and the null expected activation tuple agrees with
absence of all governance/internal activation artifacts. An enrolled vault with an empty
generation, deleted workspace/store, or unavailable registry SHALL NOT use that path.

Authored audience-bearing fields SHALL NOT enter the evaluator's reserved NUL-prefixed
namespace. A NUL in `audience` or `to_audience` SHALL produce an ERROR finding and refuse
the compile, including the values reserved for unresolved principals and the unnamed-
audience transition probe.

Policy transition previews SHALL expose the post-change ceiling for the unnamed-audience
default as `unnamed_audience_ceiling`, separately from the authored-audience
`target_ceiling`. The field SHALL be present and nullable when no concrete membership can
be evaluated.

#### Scenario: An audience no rule names receives nothing

- **WHEN** an item belongs to a scope carrying the declaration and a request arrives from
  an audience for which no standing rule matches that scope and no scope-bound grant
  applies
- **THEN** the decision is no disclosure
- **AND** its complete serialized response, including rank, top-k, pagination, order,
  graph, count, error, and governed timing fields, is identical to the same request with
  the item physically absent

#### Scenario: Non-owner inspection does not reveal a default-denied path

- **WHEN** a non-owner uses `explain` or `simulate` for one path, first while it exists and
  is denied at no disclosure and again after the same path is deleted
- **THEN** both requests receive the same error class and text
- **AND** owner inspection behaviour is unchanged
- **AND** an established terminal `release_reason` remains inspectable

#### Scenario: Reserved audience ids refuse compilation

- **WHEN** a rule or grant authors an `audience` or `to_audience` containing a NUL
- **THEN** the compiler emits an ERROR finding
- **AND** the policy compile is refused

#### Scenario: A transition preview exposes the unnamed default

- **WHEN** a declared scope has an L1 rule for `external` and a proposal removes the
  declaration
- **THEN** `target_ceiling` remains 1 for the authored audience
- **AND** `unnamed_audience_ceiling` is 6 for the post-change default

#### Scenario: A newly minted audience id is denied by default

- **WHEN** a principal's credential is rotated so it resolves to an audience id that
  appears in no policy document, and it requests an item in a declared scope
- **THEN** the decision is no disclosure
- **AND** the outcome is identical to the pre-rotation audience having been unnamed

#### Scenario: An authored rule still governs the audience it names

- **WHEN** a standing rule names an audience for a declared scope with a ceiling above no
  disclosure
- **THEN** that audience receives the rule's ceiling for that scope
- **AND** the declaration does not lower it

#### Scenario: A grant raises only the declared scope it names

- **WHEN** an audience unnamed by any standing rule holds a grant for declared scope A
  and the item also belongs to ungranted default-deny scope B
- **THEN** the grant's ceiling applies to A, B remains no disclosure, and the item remains
  no disclosure

#### Scenario: An organisation cap still lowers

- **WHEN** an organisation cap applies to a declared scope alongside a rule permitting a
  higher level
- **THEN** the lower of the two applies, unchanged by the declaration

#### Scenario: The owner reads a declared scope

- **WHEN** the owner requests an item in a declared scope for which no rule names the
  owner
- **THEN** the owner receives full release

#### Scenario: One declared scope denies across an overlapping undeclared scope

- **WHEN** an item belongs to both a declared scope and an undeclared scope, and the
  audience is named by no standing rule
- **THEN** the declared scope is L0 and the item decision is no disclosure

#### Scenario: An undeclared scope keeps today's default

- **WHEN** an item belongs only to scopes carrying no declaration and no standing rule
  matches the audience
- **THEN** the decision is full release, exactly as before this change

#### Scenario: Inspection explains a default denial

- **WHEN** the owner explains the decision for an item withheld by the declaration
- **THEN** the explanation identifies the declaring scope and every per-scope ceiling
- **AND** it does not attribute the outcome to a standing rule that does not exist
