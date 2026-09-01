## ADDED Requirements

### Requirement: Curation context remains deterministic and agent-decided

The system SHALL provide a bounded curation work item containing only recorded
page content, stable review references, deterministic measurements, current
content hashes, relevant registry identities, and the closed plan-step schemas.
The system MUST NOT use a server-side reasoning model, text generator, semantic
authority score, or language classifier to select, rank, interpret, or author a
curation plan. The active agent SHALL remain the sole author of the proposed
interpretation and steps.

#### Scenario: Agent requests context for surfaced maintenance work

- **WHEN** the agent requests a curation work item for named review refs or paths
- **THEN** Exomem returns bounded recorded context and exact bindings without
  writing a plan or deciding what the material means

#### Scenario: No model capability is installed

- **WHEN** Exomem runs in a lean standalone installation with no optional model
  runtime
- **THEN** work-item, proposal validation, preview, execution, recovery, and
  compensation remain available with identical authority semantics

### Requirement: Forward plans are strict, immutable, and exactly bound

The system SHALL accept an ordered agent-authored forward plan only through a
strict schema, canonicalize it to immutable JSON, and derive `plan_id` as the
SHA-256 digest of the canonical bytes. Proposal validation SHALL capture an
ordered target manifest, expected present content hashes, expected absent
paths, semantic draft or transition identities, relevant registry
fingerprints, deterministic postconditions, and sealed compensation material.
An agent-supplied binding that does not match current state MUST be refused and
MUST NOT be silently refreshed. A stored plan MUST be create-only; the same id
with different bytes MUST be refused as a collision.

#### Scenario: Reviewed target changed before proposal

- **WHEN** the agent submits a plan with the hash it reviewed and the live page
  now has a different hash
- **THEN** proposal creation refuses stale without storing a rebound plan

#### Scenario: Plan bytes are changed after creation

- **WHEN** a caller presents the stored plan id with different ordered steps or
  arguments
- **THEN** preview and execution refuse the identity mismatch and no step runs

### Requirement: Curation exposes only governed step kinds

The v1 forward step vocabulary SHALL be exactly `create-note`, `create-entity`,
`accept-relation`, `edit`, `supersede`, `move`, `delete`, and `recover`. Each
kind SHALL have a closed per-kind argument schema and SHALL dispatch through
the matching existing governed leaf. A plan MUST NOT contain a command name,
raw filesystem operation, callable, shell fragment, or free-form patch program.
All existing leaf confirmation, draft, content-hash, relation, source/evidence,
protected-tree, and path-confinement checks SHALL remain authoritative at plan
validation and immediately before commit.

#### Scenario: Plan requests an arbitrary command

- **WHEN** a plan step supplies a product command name or a step kind outside
  the closed vocabulary
- **THEN** proposal validation refuses the step before storing an executable plan

#### Scenario: Leaf guard changes after preview

- **WHEN** a plan previews successfully but its target hash, relation candidate,
  semantic draft, or registry changes before apply
- **THEN** the canonical leaf refuses the step and curation cannot bypass or
  weaken that refusal

### Requirement: Curation leaves typed administrative domains to their owners

The curation executor SHALL refuse direct step targets in Planning, Records,
workflow-contract storage, `_Schema`, `_Governance`, `_Adoption`, trash internals,
and every other protected administrative subtree except its own internally
written run artifacts. Planning and Records changes SHALL continue to route
only through their typed product commands, and OpenSpec lifecycle behavior
SHALL remain outside curation.

#### Scenario: Plan attempts to update a Planning item

- **WHEN** an otherwise valid edit or move step targets a Planning collection
  item
- **THEN** proposal validation refuses the target and directs the caller to the
  typed Planning owner without writing anything

### Requirement: Exact plan approval gates bounded execution

The first forward execution request SHALL require the exact `plan_id`, current
`expected_plan_fingerprint`, and a bounded single-line approval rationale. The
system SHALL revalidate the plan fingerprint and every live binding under the
vault mutation boundary before recording approval. Approval SHALL bind only
those immutable bytes. Each `apply` or `resume` request SHALL execute at most
one content step, and `resume` SHALL run only the next step of the already
approved forward or compensation plan.

#### Scenario: Confirmed plan begins execution

- **WHEN** the agent invokes apply after explicit user confirmation with the
  exact current plan identity, fingerprint, and rationale
- **THEN** Exomem durably binds approval to that plan and executes no more than
  its next uncommitted step

#### Scenario: Resume tries to switch plans

- **WHEN** a resume request names a different or newly proposed plan from the
  plan whose approval is stored
- **THEN** the request is refused and neither plan advances

### Requirement: Every committed step has exact reconstructible commit evidence

Before invoking a governed leaf, the executor SHALL persist a prepared step with
a deterministic operation id derived from plan id, ordinal, and step id.
Content-write leaves SHALL commit one content-free curation witness in the same
atomic batch as their canonical effect.

Before a `move`, `delete`, or `recover` namespace rename, the source and
destination parents SHALL already exist and be retained through held no-follow
handles; V1 curation placement MUST NOT create a knowledge or trash destination
directory as a precursor effect. After the required held-parent capability,
identity, generation, and durability preflight, the leaf SHALL first durably
publish one canonical create-only inert relocation candidate and then
one canonical create-only relocation authorization that binds the immutable
plan and approval digests to the candidate's complete canonical digest. The
candidate file, containing transition directory, and every newly created
governed-run ancestor entry SHALL be durable before the authorization is
published. The authorization file and containing transition directory SHALL be
durable before any rename. A candidate without its matching verified
authorization MUST NOT authorize an effect.

The candidate SHALL bind the run, plan, approval, ordinal, step, operation id,
source and destination, exact source hash/size/stable held-filesystem identity,
both parent identities and filesystem epoch, destination absence, complete
auxiliary-write manifest, governed lifecycle and graph-transition identities
where present, optional parent compensation identity, logical witness-time
basis, and the complete deterministic final-witness bytes and closed-terminal
projection including their digests.

`preparation_id` SHALL hash a domain separator and immutable candidate core.
`authorization_id` SHALL hash its own domain, approval digest, operation id, and
`preparation_id`. The final witness SHALL bind those ids, the logical witness-
time basis, and closed result digest. The candidate SHALL then bind the exact
final-witness bytes and their SHA-256. The authorization SHALL bind the approval
digest and approved plan membership, operation and preparation ids, SHA-256 of
the complete candidate bytes, final-witness SHA-256, and `authorization_id`.
The logical witness-time basis MUST NOT be represented as the physical rename
instant.

Before candidate publication, a missing destination parent SHALL refuse without
creating it. The held filesystem SHALL capability-probe and capture
durable monotonic namespace-generation tokens for both source and destination
parents. The tokens SHALL be restart-comparable, non-repeating within their
filesystem epoch, and SHALL change for every namespace-entry mutation including
rename followed by inverse rename. Unrelated entry mutation MAY conservatively
cause refusal. Both retained parents SHALL be durably flushed after token
capture and before candidate publication. Unsupported tokens or retained-parent
flush capability SHALL refuse before candidate publication, authorization, and
rename.

Immediately before rename the leaf SHALL revalidate its authorization, source
identity, destination absence, guards, and parent generations. After rename it
SHALL durably flush the retained source parent and then the retained destination
parent, or the shared parent once. A post-rename flush failure MUST NOT trigger
an inverse rename. Exact-target recovery SHALL repeat both idempotent parent
flushes before any auxiliary suffix. Its final witness SHALL reproduce the exact
bytes sealed by the candidate; the witness file and containing evidence
directory SHALL be durable before any terminal receipt. Exact target placement
without a final witness SHALL be adopted as the current authorized desired
placement without attributing its actor, recoverable by rolling forward only the
bound missing suffix, and MUST NOT invoke an executor rename. Exact prior
placement MAY reissue the same rename after a prior attempt only when both parent
generation tokens remain exactly at their prepared values and every guard still
matches, proving no durable namespace transition. The invariant is at most one
durable or adopted placement effect, not at most one rename syscall across power
loss. Any ambiguous placement,
identity substitution, competing or substituted transition record, missing
authorization after target placement, third-state auxiliary write, or
transition/witness/live mismatch SHALL block as
`CURATION_OUTCOME_UNCERTAIN`.
An advanced exact-prior generation, an unchanged exact-target generation, or an
unavailable, reset, non-monotonic, cross-epoch, or incomparable generation for
either placement SHALL
block as `CURATION_RENAME_HISTORY_UNPROVABLE`; no rename, rollback,
compensation, or automatic retry may proceed.

Every final witness SHALL bind the run, plan, ordinal, step, operation id, exact
before/after target manifest, governed leaf identity, result digest, and
optional parent compensation identity. Terminal attempt receipts, relocation
candidates, relocation authorizations, and approval artifacts SHALL be
create-only; at most one committed
or recovered-committed receipt SHALL exist per step; and state SHALL be
derivable from immutable verified governed evidence rather than mutable
projection state. This is governed-store authorization, not cryptographic
authentication against an actor able to forge `_Governance`; adding such a
threat model requires a separate portable signing-root design. The system MUST
NOT claim cross-file power-loss atomicity for a namespace
rename and a separate witness file.

#### Scenario: Process stops before any leaf effect

- **WHEN** a crash occurs after prepared state is durable but, for a content
  leaf, before its matching witness commits, or, for a placement leaf, before
  relocation preparation and rename
- **THEN** recovery proves no step effect committed and permits exact retry only
  if every live guard still matches

#### Scenario: Process stops after an authorized rename commits

- **WHEN** a move, delete, or recover has a durable verified relocation
  authorization, its
  source is absent, its exact destination with the prepared stable identity is
  present, the parent generations are comparable in the prepared filesystem
  epoch and show a namespace transition, and its final witness or auxiliary
  suffix is incomplete
- **THEN** recovery rolls forward only the prepared missing suffix, witness, and
  receipt
- **AND** the operation has at most one durable or adopted placement effect and
  recovery issues no rename from exact-target placement

#### Scenario: Process stops between candidate and authorization

- **WHEN** a relocation candidate is durable but its authorization is absent
- **THEN** exact prior placement with unchanged parent generations may recreate
  only the deterministic authorization after every live guard is revalidated
- **AND** target or ambiguous placement blocks without rename or roll-forward

#### Scenario: Prepared relocation placement is ambiguous

- **WHEN** a relocation transition exists but both paths, neither path, a
  substituted identity, missing authorization after target placement, or an
  auxiliary third state is observed
- **THEN** the run blocks with `CURATION_OUTCOME_UNCERTAIN`
- **AND** no rename, rollback, retry, or compensation proceeds automatically

#### Scenario: Placement has unprovable rename history

- **WHEN** an exact-prior placement has a changed parent generation, an exact-
  target placement has an unchanged parent generation, or either placement has
  a reset, unavailable, non-monotonic, cross-epoch, or incomparable token
- **THEN** the run blocks with `CURATION_RENAME_HISTORY_UNPROVABLE`
- **AND** no rename, rollback, retry, or compensation proceeds automatically

#### Scenario: Process stops after the leaf commit

- **WHEN** a crash occurs after a content effect and matching witness commit, or
  after a placement effect and matching final witness, but before the terminal
  step receipt is stored
- **THEN** read-only status reports that exact recovery is required, and the
  next resume verifies the witness and live postcondition, writes one
  recovered-committed receipt, and never invokes the leaf again

#### Scenario: Evidence and live state disagree

- **WHEN** a witness is invalid, competing, or inconsistent with the exact live
  postcondition
- **THEN** the run becomes blocked with `CURATION_OUTCOME_UNCERTAIN` and no
  automatic retry or compensation proceeds

### Requirement: Run phases report partial truth and exact replay

The system SHALL derive run phase from the immutable plan, create-only approval,
verified relocation candidates and authorizations, witnesses, and terminal
receipts and SHALL expose at least `proposed`, `approved`, `executing`,
`partial`, `failed`, `completed`, `blocked`, `compensating`, `compensation-partial`,
and `compensated`. It MUST NOT report a multi-step atomic commit. Exact replay
SHALL return already-committed step outcomes without executing their leaves;
changed plan bytes, stale bindings, or an unprovable outcome SHALL refuse.

#### Scenario: Third step fails after two commits

- **WHEN** two ordered steps have committed receipts and the third cleanly fails
- **THEN** status reports `partial`, identifies both committed steps and the
  failed next step, and preserves the exact next permitted action

#### Scenario: Completed apply is replayed

- **WHEN** the same approved plan and next-step request is repeated after its
  terminal receipt exists
- **THEN** Exomem returns the stored outcome and creates no second content effect

### Requirement: Compensation is a separate reviewed history-preserving plan

The system SHALL derive compensation only from committed step receipts,
matching witnesses, and sealed pre-step material. It SHALL create a new
immutable compensation plan linked to the forward plan and reverse committed
steps in descending order. Creation effects SHALL move to governed trash;
deletions SHALL recover their exact trash entries; moves SHALL use a guarded
inverse move; edits and accepted relations SHALL create a superseding
correction from the sealed pre-step document; supersessions SHALL be corrected
by superseding the committed successor; and recovered items SHALL be deleted
through the governed delete leaf. Compensation SHALL require its own exact
fingerprint and approval rationale and SHALL preserve all forward and reverse
plans, witnesses, receipts, trash history, and supersession chains.

#### Scenario: User approves compensation after a partial run

- **WHEN** a forward run committed two steps before failing and the user reviews
  and approves the derived compensation plan
- **THEN** compensation processes only those committed effects in reverse order
  through governed leaves and leaves the original evidence intact

#### Scenario: Later work changed a compensation target

- **WHEN** a target no longer matches the sealed forward receipt at compensation
  proposal or apply time
- **THEN** compensation refuses stale rather than overwriting the later work

### Requirement: Plan execution is content-language-neutral

The curation plan, work item, receipts, and validation paths SHALL preserve
Unicode text and SHALL determine executability from typed step ids, operation
schemas, paths, hashes, draft tokens, and registry ids rather than English
keywords or monolingual entailment labels. Existing ASCII slug rules MAY still
govern filenames while Unicode titles and bodies remain unchanged.

#### Scenario: Multilingual plan is reviewed and applied

- **WHEN** an agent proposes a typed correction whose title and body contain
  non-English or mixed-language Unicode text
- **THEN** the same validation, fingerprint, approval, receipt, recovery, and
  compensation contract applies without language-specific fallback
