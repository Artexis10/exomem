## MODIFIED Requirements

### Requirement: Curation exposes only governed step kinds

The v1 forward step vocabulary SHALL be exactly `create-note`, `create-entity`,
`accept-relation`, `edit`, `supersede`, `move`, `repoint-links`, `delete`, `recover`, `fold-duplicate`, and `fold-recap-revisions`. Each
kind SHALL have a closed per-kind argument schema and SHALL dispatch through
the matching governed leaf. The two fold kinds are the only steps that retire a Source or Evidence page, and they dispatch through their own leaf, so `supersede`, `move` and `delete` keep refusing Sources and Evidence. A plan MUST NOT contain a command name,
raw filesystem operation, callable, shell fragment, or free-form patch program.
A `move` step SHALL accept an optional `fields` patch. Each key of that patch
SHALL be a field that the `page-fields` registry declares with `role: pointer`,
and proposal SHALL refuse any other key with `CURATION_STEP_INVALID`. A `move`
step SHALL NOT take `update_wikilinks`: the plan's `repoint-links` step
rewrites links.
An `edit` step whose operation is `patch_frontmatter` MAY target a page in
`Sources/` or `Evidence/` only for a field that the `page-fields` registry
declares with `role: pointer`. That step SHALL run as the governed pointer
write that corrects a pointer field in place. The plan approval SHALL be the
write's approval. The step SHALL set no `updated`, and its sealed postcondition
SHALL be its exact after bytes. Proposal SHALL refuse the same operation on any
other field of such a page with `APPEND_ONLY`.
All existing leaf confirmation, draft, content-hash, relation, source/evidence,
protected-tree, and path-confinement checks SHALL remain authoritative at plan
validation and immediately before commit. Except in a pointer correction on an
append-only page, an edit's transition alone SHALL be minted when its step runs, never taken from the plan, so the edited page
carries the commit instant in `updated` and the transition's age never refuses
an approved edit. Before any byte is written, the executor SHALL refuse with
`CURATION_BINDING_STALE` a planned page that, with `updated` set back to the
reviewed instant, differs from the sealed postcondition. Proposal SHALL refuse
with `RELATION_REVIEW_NOT_APPLICABLE` a step that gives a relation disposition
when its leaf's validation reports a fully governed page that needs none,
because the leaf refuses that review at commit; it ignores a review on any
other page. Proposal SHALL also refuse with `SEMANTIC_CONTRACT_BLOCKED` a step
whose leaf validation reports a blocking finding that the step does not answer,
because the leaf refuses that step at commit. This covers create, supersede
and edit steps. The step's own relation review answers a relation-disposition
finding. When the step writes a link to a page that an earlier step of the plan
creates, the relation-disposition judgement moves to execution, because the leaf
can judge that link only once the page exists; if the link does not qualify, the
leaf still refuses the step there.

#### Scenario: Plan requests an arbitrary command

- **WHEN** a plan step supplies a product command name or a step kind outside
  the closed vocabulary
- **THEN** proposal validation refuses the step before storing an executable plan

#### Scenario: Leaf guard changes after preview

- **WHEN** a plan previews successfully but its target hash, relation candidate,
  semantic draft, or registry changes before apply
- **THEN** the canonical leaf refuses the step and curation cannot bypass or
  weaken that refusal

#### Scenario: Plan gives a review the page does not need

- **WHEN** a step gives `reviewed_none` for a page whose leaf validation does
  not require it, such as a page that already links to an existing page
- **THEN** proposal refuses with `RELATION_REVIEW_NOT_APPLICABLE` and stores no
  plan

#### Scenario: Plan omits a review the page needs

- **WHEN** a create step has no qualifying relation and no review, or an edit
  step changes a page so that its saved relation review is no longer current
  and gives no new review
- **THEN** proposal refuses with `SEMANTIC_CONTRACT_BLOCKED` and stores no plan

#### Scenario: Edit approved days after proposal

- **WHEN** an edit step is applied more than a day after proposal and its
  target and link targets are unchanged
- **THEN** the edit applies the reviewed change and the page's `updated` is the
  commit instant

#### Scenario: Edit renders differently at approval

- **WHEN** another writer changes what an approved edit step renders, such as
  creating the target of one of its links
- **THEN** the step refuses with `CURATION_BINDING_STALE`, nothing is written,
  and the run records the refusal as not retryable

#### Scenario: Plan names a fold step

- **WHEN** a plan step has kind `fold-duplicate` or `fold-recap-revisions` with its closed arguments
- **THEN** proposal accepts it and the work item lists both schemas

#### Scenario: Plan deletes a Source

- **WHEN** a plan step has kind `delete` or `supersede` and targets a Source or Evidence page
- **THEN** the step is refused exactly as before this capability

#### Scenario: A plan corrects pointer fields on Sources

- **WHEN** a plan carries `edit` steps whose `patch_frontmatter` operations
  change the `project` field of several Sources, and the owner approves it
- **THEN** each Source differs only in its `project` value span, with
  `updated` unchanged
- **AND** each step's evidence holds its pointer-write record

#### Scenario: A rehome patches a field that is not a pointer

- **WHEN** a `move` step's `fields` patch names a field that the `page-fields`
  registry does not declare as a pointer, such as `title`
- **THEN** proposal refuses with `CURATION_STEP_INVALID` and stores no plan

### Requirement: A plan's later steps may link to its own earlier pages

Proposal SHALL render a later step's links to a page that an earlier step of
the same plan creates as the writer renders a link to an existing page, so the
sealed draft matches at execution. Only the writer's link resolution SHALL be
projected; every other check SHALL read the vault as it is, and a page that
another writer creates, edits or removes after proposal SHALL still make the
affected step refuse through its leaf. Proposal SHALL refuse with `CURATION_STEP_CONFLICT`
these plans, which could not then run as sealed:

- a later step bound to a path that an earlier step writes, except a
  `repoint-links` step, which SHALL bind a page that an earlier `move` step
  writes, or that an earlier `edit` step writes as a pointer correction of an
  append-only page, at that step's sealed after hash;
- a step that links to a page an earlier step supersedes;
- a step that gives a relation disposition while it links to a page an earlier
  step creates, because that link is its relation once the page exists.

A later step's other dependencies on an earlier step's effects, such as a link
by a title that an earlier edit changes, are not projected; such a step
refuses through its leaf when it runs.

#### Scenario: Split note links its second page to its first

- **WHEN** an approved plan creates a page and a later step creates a page
  whose content links to it by path or by title
- **THEN** every step applies in order and the later page holds the link in the
  form the writer renders for an existing page

#### Scenario: Another writer creates the link target after proposal

- **WHEN** a sealed step links to a page that did not exist at proposal and
  that no earlier step of its plan creates, and another writer creates it
- **THEN** that step refuses through its leaf and writes nothing

#### Scenario: Plan a projection cannot make run

- **WHEN** a later step other than `repoint-links` is bound to a path an
  earlier step writes, links to a page an earlier step supersedes, or gives a
  relation disposition while it links to a page an earlier step creates
- **THEN** proposal refuses with `CURATION_STEP_CONFLICT` and stores no plan

#### Scenario: A rehome corrects a Source that links to a moved note

- **WHEN** a plan moves a note, corrects the `project` field of a Source that
  links to it through an `edit` step, and ends with `repoint-links`
- **THEN** proposal accepts the plan, and `repoint-links` binds the Source at
  the sealed after hash of the `edit` step that corrected it

#### Scenario: Moved pages link to each other

- **WHEN** a plan moves two pages that link to each other and ends with
  `repoint-links`
- **THEN** proposal accepts the plan, and `repoint-links` binds each moved page
  at the sealed after hash of the step that moved it

### Requirement: Every committed step has exact reconstructible commit evidence

Before invoking a governed leaf, the executor SHALL persist a prepared step with
a deterministic operation id equal to the SHA-256 digest of canonical JSON for
the domain-separated tuple `["exomem-curation-operation-v1", plan_id, ordinal,
step_id]`. The encoding SHALL preserve the ordinal as a number and MUST NOT use
ambiguous field concatenation.

Content-write leaves SHALL commit one content-free curation witness in the same
atomic batch as their canonical effect. A `move` step SHALL commit its witness
in the batch that writes the moved page's destination bytes, and SHALL publish
its intent record before its rename, as the requirement "A move step records
its intent before its rename and recovers without renaming" states.

`delete` and `recover` steps remain valid in the plan schema so a reviewer can
express them, but the executor SHALL refuse them. The refusal SHALL occur at
proposal, preview, and apply, before any record is published and before any
rename is attempted, and SHALL be reported as
`CURATION_RENAME_HISTORY_UNPROVABLE`. A refused step SHALL leave no rename, no
rollback, no trash write, no intent record, no witness, and no terminal
receipt, and SHALL create no governed run store.

Every curation witness SHALL bind the run, plan, ordinal, step, operation id,
exact before/after target manifest, governed leaf identity, result digest, and
optional parent compensation identity. Terminal attempt receipts and approval
artifacts SHALL be create-only; at most one committed
or recovered-committed receipt SHALL exist per step; and state SHALL be
derivable from immutable verified governed evidence rather than mutable
projection state. This is governed-store authorization, not cryptographic
authentication against an actor able to forge `_Governance`; adding such a
threat model requires a separate portable signing-root design.

#### Scenario: Process stops before any leaf effect

- **WHEN** a crash occurs after prepared state is durable but, for a content
  leaf, before its matching witness commits
- **THEN** recovery proves no step effect committed and permits exact retry only
  if every live guard still matches

#### Scenario: Reviewed plan contains a relocation step

- **WHEN** a plan containing a `delete` or `recover` step is proposed,
  previewed, or applied
- **THEN** the request fails with `CURATION_RENAME_HISTORY_UNPROVABLE` before any
  record is published and before any rename is attempted
- **AND** no rename, rollback, trash write, intent record, witness, terminal
  receipt, or governed run store is created

#### Scenario: Process stops after the leaf commit

- **WHEN** a crash occurs after a content effect and matching witness commit
  but before the terminal step receipt is stored
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
verified move intent records, witnesses, and terminal
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
a pointer correction on an append-only page SHALL be reversed through the
governed pointer write's revert;
deletions SHALL recover their exact trash entries; a move SHALL be reversed by
a guarded inverse move that restores the page's sealed pre-step bytes, so its
lineage entry and its `fields` patch are removed and no lineage entry is
added; a `repoint-links` step SHALL be reversed by returning each page that it
rewrote to its sealed pre-step bytes, and each append-only page through the
governed pointer write's revert; other edits and accepted relations SHALL create a superseding
correction from the sealed pre-step document; supersessions SHALL be corrected
by superseding the committed successor; and recovered items SHALL be deleted
through the governed delete leaf. Compensation SHALL require its own exact
fingerprint and approval rationale and SHALL preserve all forward and reverse
plans, witnesses, receipts, trash history, and supersession chains. A fold step SHALL be reversed from its sealed manifest and witness: each page that it rewrote returns to its sealed pre-step bytes, the copy's page returns from its sealed pre-step document, the history entries that it created move to trash, and the original bytes or revision files that it moved are recovered from their exact trash entries to their original paths.

#### Scenario: User approves compensation after a partial run

- **WHEN** a forward run committed two steps before failing and the user reviews
  and approves the derived compensation plan
- **THEN** compensation processes only those committed effects in reverse order
  through governed leaves and leaves the original evidence intact

#### Scenario: Later work changed a compensation target

- **WHEN** a target no longer matches the sealed forward receipt at compensation
  proposal or apply time
- **THEN** compensation refuses stale rather than overwriting the later work

#### Scenario: A duplicate fold is undone

- **WHEN** the owner approves the compensation plan of an applied `fold-duplicate` step
- **THEN** the canonical page, the copy's page, every rewritten inbound page and the copy's original bytes equal their pre-step bytes
- **AND** later work on any of those pages makes compensation refuse stale instead of overwriting it

#### Scenario: A recap fold is undone

- **WHEN** the owner approves the compensation plan of an applied `fold-recap-revisions` step
- **THEN** every revision file is back at its path with its pre-step bytes and the step's history entries are in trash
- **AND** every citation of those revisions resolves

#### Scenario: A pointer correction is undone

- **WHEN** the owner approves the compensation plan of an applied plan that
  corrected the `project` field of several Sources
- **THEN** each Source hashes to its recorded before hash
- **AND** no superseding correction is created

#### Scenario: A rehome is undone

- **WHEN** the owner approves the compensation plan of an applied plan that
  moved three pages with a `project` patch and ended with `repoint-links`
- **THEN** each moved page is back at its old path with its pre-step bytes,
  each rewritten referrer holds its pre-step bytes, and each repointed
  append-only page hashes to its recorded before hash

### Requirement: Fold steps move only bytes that remain held elsewhere

The refusal of `delete` and `recover` for unprovable relocation history SHALL stay in force. The two fold kinds are not those kinds. They SHALL move a file to the vault trash only after the step has re-verified, immediately before the move, that identical bytes remain held elsewhere: the canonical page's original for `fold-duplicate`, the history entry for `fold-recap-revisions`. They SHALL refuse the move when that verification fails. An uncertain rename state therefore cannot lose content, which is why the fold leaf does not need namespace lineage. When a crash leaves the state unknown, resume SHALL decide from the live state: bytes at the recorded trash identity mean the move committed; bytes in place mean it did not; neither or both SHALL block the run with `CURATION_OUTCOME_UNCERTAIN`.

#### Scenario: The retained copy vanishes before the move

- **WHEN** the canonical page's original is unreadable when apply reaches the move
- **THEN** the step refuses, moves nothing and leaves the copy in place

#### Scenario: The plain relocation kinds stay refused

- **WHEN** a plan contains `delete` or `recover`
- **THEN** the executor still refuses it with `CURATION_RENAME_HISTORY_UNPROVABLE`

## ADDED Requirements

### Requirement: A move step records its intent before its rename and recovers without renaming

Immediately before the rename of a `move` step, the executor SHALL publish and
flush one create-only intent record. The record SHALL bind the operation id,
the old and new paths, the sealed before hash, and the sealed rename-after
state: the old path absent and the new path holding the sealed before bytes.
The executor SHALL publish the record from a seam that runs once per step, not
from a hook that the child batch also calls.

Recovery MUST NOT issue a rename. It SHALL decide from the intent record and
the live state:

- with no intent record and the page at its old path with the sealed bytes,
  the step SHALL be retried exactly when every guard still matches;
- with an intent record and the page at its old path with the sealed bytes,
  the run SHALL block with `CURATION_RENAME_HISTORY_UNPROVABLE`, and the owner
  re-proposes the remaining work;
- with an intent record, the old path absent and the new path holding the
  sealed before bytes, recovery SHALL adopt the rename and roll the step
  forward: it SHALL commit the destination bytes, the lineage entry, the
  `fields` patch and the witness in one batch, and SHALL publish the semantic,
  catalog, graph and index effects of the move without a rename;
- with a witness and a new path that match the sealed postcondition, the step
  SHALL be recovered as committed through the witness;
- with the page at its old path holding other bytes, the step SHALL refuse
  with `CURATION_BINDING_STALE`;
- in any other state, including the page at neither path or at both, the run
  SHALL block with `CURATION_OUTCOME_UNCERTAIN`.

In store mode, a move SHALL commit in one store transaction, and recovery
SHALL read the outcome from the store.

#### Scenario: Process stops after the rename

- **WHEN** a crash occurs after a `move` step's rename and before its batch
- **THEN** status reports that the step needs a roll forward
- **AND** the next resume adopts the rename, commits the batch, and leaves the
  page indexed, cataloged and graphed at the new path with one lineage entry
- **AND** no second rename runs

#### Scenario: Someone renames the page back during the crash window

- **WHEN** a crash occurs after the intent record and the page is found at its
  old path with the sealed bytes
- **THEN** the run blocks with `CURATION_RENAME_HISTORY_UNPROVABLE` and no rename
  runs

#### Scenario: Process stops before the intent record

- **WHEN** a crash occurs before the intent record is durable
- **THEN** the next resume retries the step exactly when every guard still
  matches

#### Scenario: The page is at both paths

- **WHEN** recovery finds the page's bytes at the old path and at the new path
- **THEN** the run blocks with `CURATION_OUTCOME_UNCERTAIN` and nothing changes

### Requirement: A repoint-links step rewrites the plan's referrers once

A `repoint-links` step SHALL take no arguments, SHALL be the last step of its
plan, and SHALL appear at most once. Proposal SHALL refuse with
`CURATION_STEP_INVALID` a `repoint-links` step that is not last, that repeats,
or that follows no `move` step.

At proposal, the step SHALL seal every referrer of every earlier `move` step
with its exact before hash and its rewritten bytes. A referrer that an earlier
`move` step writes, or that an earlier `edit` step corrects as a pointer write,
SHALL be bound at that step's sealed after hash. The step
SHALL give each referrer exactly one outcome, as a move preview does:
`rewritten`, `pointer-repointed (append-only page)`,
`unchanged: resolves via prior path`, `unchanged: keeps the old path`, or
`refused`. A `refused` outcome SHALL
refuse the plan at proposal.

At execution, the step SHALL write every rewritten page and every pointer
repoint in one atomic batch with its witness. Each append-only page SHALL
change only through the governed pointer write, and the plan approval SHALL be
that write's approval. A referrer whose bytes differ from its sealed binding
SHALL refuse the step with `CURATION_BINDING_STALE`, and nothing SHALL be
written.

#### Scenario: Three notes are rehomed with a Source referrer

- **WHEN** an approved plan moves three notes to another project's folder with
  a `project` patch and ends with `repoint-links`, and a Source links one of
  them by path
- **THEN** each note is at its new path with one lineage entry and the new
  `project`
- **AND** each writable referrer links the new paths, the Source's link names
  the new path, and every other byte of the Source is unchanged

#### Scenario: A referrer changes after proposal

- **WHEN** another writer edits a sealed referrer before `repoint-links` runs
- **THEN** the step refuses with `CURATION_BINDING_STALE`, writes nothing, and
  every old link still resolves through lineage

### Requirement: Curation teaches its relocation plan shapes

`read_instructions(topic="curation.propose")` SHALL return worked plan
examples: a split whose child notes each declare their `project`, a single
`move` with its per-referrer preview outcomes, and a rehome of several notes
with a `fields` patch, `edit` steps that correct the `project` field of the
Sources that belong with them, and a final `repoint-links` step. Each example SHALL pass
proposal validation against the shipped plan schema.

#### Scenario: An agent asks how to rehome notes

- **WHEN** a connected agent reads the `curation.propose` instructions
- **THEN** the response holds a rehome example
- **AND** proposal accepts that example's plan in a vault that holds the pages
  it names
