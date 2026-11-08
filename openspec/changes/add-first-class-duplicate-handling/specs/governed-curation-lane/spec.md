## MODIFIED Requirements

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

## ADDED Requirements

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

Proposal SHALL compute the pinned revisions: those that any live page outside the episode's own pages references, by `sources:` citation, `ingested_into` back-reference or link. It SHALL compute them over the whole vault, regardless of what the caller can see, and SHALL refuse to run without the same whole-namespace content authority that source-identity lookup requires. It SHALL seal the pinned revisions as kept and SHALL refuse a plan that retires one.

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
