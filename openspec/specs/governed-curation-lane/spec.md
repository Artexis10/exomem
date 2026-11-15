# governed-curation-lane Specification

## Purpose
Carry an agent's reviewed multi-step vault curation through one immutable plan: deterministic work items, exact plan approval, bounded one-step execution, reconstructible commit evidence for every step, and a separate reviewed compensation plan.

## Requirements

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
paths, semantic draft identities, edit transitions with their reviewed
postconditions, relevant registry fingerprints, deterministic postconditions, and sealed compensation material.
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
`accept-relation`, `edit`, `supersede`, `move`, `delete`, `recover`, `fold-duplicate`, and `fold-recap-revisions`. Each
kind SHALL have a closed per-kind argument schema and SHALL dispatch through
the matching governed leaf. The two fold kinds are the only steps that retire a Source or Evidence page, and they dispatch through their own leaf, so `supersede`, `move` and `delete` keep refusing Sources and Evidence. A plan MUST NOT contain a command name,
raw filesystem operation, callable, shell fragment, or free-form patch program.
All existing leaf confirmation, draft, content-hash, relation, source/evidence,
protected-tree, and path-confinement checks SHALL remain authoritative at plan
validation and immediately before commit. An edit's transition alone SHALL be
minted when its step runs, never taken from the plan, so the edited page
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

### Requirement: A plan's later steps may link to its own earlier pages

Proposal SHALL render a later step's links to a page that an earlier step of
the same plan creates as the writer renders a link to an existing page, so the
sealed draft matches at execution. Only the writer's link resolution SHALL be
projected; every other check SHALL read the vault as it is, and a page that
another writer creates, edits or removes after proposal SHALL still make the
affected step refuse through its leaf. Proposal SHALL refuse with `CURATION_STEP_CONFLICT`
these plans, which could not then run as sealed:

- a later step bound to a path that an earlier step writes;
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

- **WHEN** a later step is bound to a path an earlier step writes, links to a
  page an earlier step supersedes, or gives a relation disposition while it
  links to a page an earlier step creates
- **THEN** proposal refuses with `CURATION_STEP_CONFLICT` and stores no plan

### Requirement: Every committed step has exact reconstructible commit evidence

Before invoking a governed leaf, the executor SHALL persist a prepared step with
a deterministic operation id equal to the SHA-256 digest of canonical JSON for
the domain-separated tuple `["exomem-curation-operation-v1", plan_id, ordinal,
step_id]`. The encoding SHALL preserve the ordinal as a number and MUST NOT use
ambiguous field concatenation.

Content-write leaves SHALL commit one content-free curation witness in the same
atomic batch as their canonical effect.

Relocation steps (`move`, `delete`, `recover`) remain valid in the plan schema
so a reviewer can express them, but until the held filesystem exposes durable
monotonic parent-namespace generation tokens the executor SHALL refuse them.
The refusal SHALL occur at proposal, preview, and apply, before any transition
record is published and before any rename is attempted, and SHALL be reported as
`CURATION_RENAME_HISTORY_UNPROVABLE`. A refused relocation step SHALL leave no
rename, no rollback, no trash write, no relocation candidate or authorization,
no witness, and no terminal receipt, and SHALL create no governed run store. The
prepared-relocation protocol that would lift this refusal is specified by the
`add-curation-relocation-protocol` change and is out of scope for v1.

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

- **WHEN** a plan containing a `move`, `delete`, or `recover` step is proposed,
  previewed, or applied on a filesystem without durable parent-namespace
  generation tokens
- **THEN** the request fails with `CURATION_RENAME_HISTORY_UNPROVABLE` before any
  transition record is published and before any rename is attempted
- **AND** no rename, rollback, trash write, relocation candidate, authorization,
  witness, terminal receipt, or governed run store is created

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

### Requirement: A duplicate fold step retires one proven-identical copy and seals its manifest

A `fold-duplicate` step SHALL have a closed argument schema of `canonical` and `copy`, each with a vault-relative `path` and the page's `expected_hash`, and a single-line `why`. Proposal SHALL seal a binding manifest of every page and file the fold touches: the canonical page and the copy's page with their content hashes, the copy's original bytes with their SHA-256 and size, each inbound page that the fold rewrites with its content hash, the exact frontmatter fields it merges, and the expected-absent trash path. Preview SHALL show the canonical choice, the merged families, import mappings and back-references, every inbound rewrite as a before and after, the bytes that move to trash, and the undo that compensation offers. The step SHALL apply the fold that `duplicate-handling` defines and SHALL refuse at proposal, preview and apply each condition that capability names. A changed binding SHALL refuse with `CURATION_BINDING_STALE` and write nothing.

Apply SHALL commit one content-free witness that binds the exact before and after of every page the step changed and the trash identity of the moved bytes, so that the witness and the sealed manifest suffice for compensation.

#### Scenario: Preview names every change

- **WHEN** an agent previews a plan with one `fold-duplicate` step
- **THEN** the preview lists the canonical page, the copy, the merged fields, each rewritten inbound page and the bytes to trash
- **AND** no file changes

#### Scenario: A page changed after preview

- **WHEN** an inbound page named in the manifest changes between preview and apply
- **THEN** the step refuses with `CURATION_BINDING_STALE` and writes nothing

#### Scenario: A plan folds several copies of one original

- **WHEN** a plan holds two `fold-duplicate` steps that fold two copies into one canonical page
- **THEN** both run in order and the canonical page lists every family and mapping of both copies

### Requirement: A recap fold moves an episode's revisions into history and keeps every referenced revision

A `fold-recap-revisions` step SHALL have a closed argument schema of the episode's head page and its `expected_hash`, and the ordered list of that episode's superseded revision files, each with `path` and `expected_hash`. Proposal SHALL refuse a list that holds any file outside one episode and one audience group, any page that is not a superseded recap revision, or a head that is not the episode's single live page.

Proposal SHALL compute the pinned revisions: those that any live page outside the episode's own pages references, by `sources:` citation, `ingested_into` back-reference or link, whatever its lifecycle class: a superseded or retired page still cites the text it cited. It SHALL compute them over the whole vault, regardless of what the caller can see, and SHALL refuse to run without the same whole-namespace content authority that source-identity lookup requires. It SHALL seal the pinned revisions as kept and SHALL refuse a plan that retires one.

Apply SHALL, for the episode:

1. Write each superseded revision, oldest first, as a create-only compiled-history entry of the head, holding the revision's exact stored bytes, skipping an entry that already exists for that revision.
2. Keep each pinned revision as its file, with its superseded mark, so every reference to it resolves to the text that it cited.
3. Re-read each remaining revision against its history entry and move each one whose bytes are identical to the vault trash.

The step SHALL NOT change the head. It SHALL seal the history entry paths as expected-absent and the trash identities in its manifest.

#### Scenario: Six revisions of one episode, one of them cited

- **WHEN** a plan folds an episode whose head has five superseded revisions and a live compiled page cites one of them
- **THEN** the head has five history entries in recording order
- **AND** the cited revision remains a file and four revision files are in trash
- **AND** the citation resolves before, during and after the step

#### Scenario: A reference the caller cannot see

- **WHEN** a revision is cited only by a page withheld from the caller
- **THEN** proposal still pins that revision, or refuses for lack of whole-namespace authority
- **AND** it never plans to retire a revision that any live page references

#### Scenario: A history entry is missing at trash time

- **WHEN** the history entry of a revision cannot be read back byte-identical
- **THEN** the step refuses to move that revision to trash and leaves the file in place

#### Scenario: An interrupted step resumes

- **WHEN** a step stops after writing some history entries and moving some revision files
- **THEN** resume completes the remaining entries and moves without duplicating an entry or touching a moved file

### Requirement: Fold steps move only bytes that remain held elsewhere

The refusal of `move`, `delete` and `recover` for unprovable relocation history SHALL stay in force. The two fold kinds are not those kinds. They SHALL move a file to the vault trash only after the step has re-verified, immediately before the move, that identical bytes remain held elsewhere: the canonical page's original for `fold-duplicate`, the history entry for `fold-recap-revisions`. They SHALL refuse the move when that verification fails. An uncertain rename state therefore cannot lose content, which is why the fold leaf does not need namespace lineage. When a crash leaves the state unknown, resume SHALL decide from the live state: bytes at the recorded trash identity mean the move committed; bytes in place mean it did not; neither or both SHALL block the run with `CURATION_OUTCOME_UNCERTAIN`.

#### Scenario: The retained copy vanishes before the move

- **WHEN** the canonical page's original is unreadable when apply reaches the move
- **THEN** the step refuses, moves nothing and leaves the copy in place

#### Scenario: The plain relocation kinds stay refused

- **WHEN** a plan contains `delete` or `move`
- **THEN** the executor still refuses it with `CURATION_RENAME_HISTORY_UNPROVABLE`

### Requirement: A fold reads references from the disk and rereads only the pages that changed

A fold SHALL find the pages that cite a revision or a copy by a wikilink or by a memory reference to its identity, over every page the vault walk yields and with no caller filter. It SHALL judge those pages by what the disk holds, not by what the freshness registry has been told, so that a page written outside the service counts before the watcher reports it. The memory-reference part of that census SHALL stat every page and read only the pages whose stat signature moved since their last read, so that sealing and applying a fold hold the vault mutation boundary for a stat walk that does not read the pages. When the registry's freshness key differs from the disk's, the wikilink part SHALL read the pages instead of the resident inbound-link index. A step that resolves `ingested_into` entries SHALL use the writers' shared resolver and SHALL NOT build a resolver of its own.

#### Scenario: A repeat census reads only what changed

- **WHEN** a fold step runs its census a second time after one page was edited
- **THEN** the second census reads that one page and no other

#### Scenario: A citation added after the proposal still pins

- **WHEN** a page cites a revision by memory reference between the proposal and the apply
- **THEN** apply refuses with `CURATION_BINDING_STALE` and moves nothing

#### Scenario: A citer written outside the service pins before the watcher reports it

- **WHEN** a page that the registry has not heard of, or an edit that it has not heard of, cites a revision by memory reference or by wikilink between the proposal and the apply, with the registry live
- **THEN** apply refuses with `CURATION_BINDING_STALE` and moves nothing
