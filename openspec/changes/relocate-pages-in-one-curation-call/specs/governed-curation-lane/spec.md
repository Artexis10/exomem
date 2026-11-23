## MODIFIED Requirements

### Requirement: Exact plan approval gates bounded execution

The first forward execution request SHALL require the exact `plan_id`, current
`expected_plan_fingerprint`, and a bounded single-line approval rationale. The
system SHALL revalidate the plan fingerprint and every live binding under the
vault mutation boundary before recording approval. Approval SHALL bind only
those immutable bytes. Each `apply` or `resume` request SHALL run the approved
plan's uncommitted steps in order, and `resume` SHALL run only the approved
forward or compensation plan.

#### Scenario: Confirmed plan begins execution

- **WHEN** the agent invokes apply after explicit user confirmation with the
  exact current plan identity, fingerprint, and rationale
- **THEN** Exomem durably binds approval to that plan and runs its uncommitted
  steps in order until the run ends or the request budget is spent

#### Scenario: Resume tries to switch plans

- **WHEN** a resume request names a different or newly proposed plan from the
  plan whose approval is stored
- **THEN** the request is refused and neither plan advances

### Requirement: A preview that creates or moves notes asks the ownership question once

A curation preview whose agent-written plan holds `create-note` or `move` steps
for compiled notes SHALL carry one ownership question. The question SHALL name
each such step by its step id with the project the step assigns, state the
ownership test and the deferral rule from the guidance routes, and tell the
agent to correct a step's project before the user confirms. The question SHALL
NOT block the preview, change a step, or alter the plan's fingerprint.

#### Scenario: A split's children are each named

- **WHEN** a plan splits one note into two `create-note` steps, each with the project key `project-alpha`
- **THEN** the preview carries one ownership question that names both step ids with `project-alpha`
- **AND** the plan's fingerprint is the same as without the question

#### Scenario: A plan without note placement carries no question

- **WHEN** a plan holds only `edit` steps
- **THEN** the preview carries no ownership question

#### Scenario: A relocation names its project

- **WHEN** the agent previews a plan that a relocation derived
- **THEN** the preview carries no ownership question

## ADDED Requirements

### Requirement: A request runs as many steps as its budget allows

A request SHALL run one step at a time. Each step SHALL hold the vault mutation
boundary as hosted-mutation-safety's "Curation Holds The Boundary Only At
Canonical Seams" states, and its derived fan-out SHALL run before the next step
starts. A request SHALL stop starting steps when the rest of its budget would
not cover its slowest step so far plus the delivery reserve. It SHALL then
answer `executing` with `progress`: the plan's steps, the committed steps and
the steps this request ran.

#### Scenario: A run spans several requests

- **WHEN** an approved plan's steps take longer than one request budget
- **THEN** apply answers `executing` with `progress`
- **AND** each resume continues from the first uncommitted step, and the run
  completes in fewer requests than it has steps

#### Scenario: A foreground write arrives during a run

- **WHEN** another write waits for the vault mutation boundary while a step holds it
- **THEN** that write commits before the next step commits

#### Scenario: A step reads what the step before it wrote

- **WHEN** one request runs a step that binds a page which the step before it rewrote
- **THEN** the step finds its binding current and commits

#### Scenario: The process stops during a run

- **WHEN** the process stops after some steps of a request committed
- **THEN** a resume runs the remaining steps, and no step is lost or repeated

### Requirement: A request that already holds the boundary runs its steps in that hold

A request that already holds the vault mutation boundary, as the episode
workflow and a revert do, SHALL run every step inside that one hold. Its
derived fan-out SHALL run once, when the hold ends, and not after each step.

#### Scenario: A revert runs several steps

- **WHEN** a revert runs a compensation plan of several steps
- **THEN** the steps run inside the revert's one hold, and the fan-out runs
  when that hold ends

### Requirement: A refusal after committed steps stops the request

A refusal of a request's first step SHALL be raised. After the request
committed steps, no refusal SHALL be raised. The request SHALL answer its
`progress` and `stopped`: the code, the reason, the stopped step and
`committed`. `committed` SHALL be true when the request knows the step's leaf
committed, false when it recorded the step refused, and null otherwise. The
next resume SHALL settle a null step from its witness.

#### Scenario: A busy vault refuses a request's first step

- **WHEN** the first step of a request meets a busy vault
- **THEN** the request answers `MUTATION_BUSY` as an error and commits no step

### Requirement: A stopped request names the run's next action

After committed steps, `CURATION_RUN_BUSY` and `MUTATION_BUSY` SHALL keep the
phase `executing`, with `next_action` `resume`. Any other refusal after
committed steps SHALL return `stopped` with an honest `partial` or `blocked`
phase, and SHALL NOT be raised. An agent SHALL follow the answer's
`next_action`.

#### Scenario: A busy vault stops a request after a committed step

- **WHEN** a request committed a step and the next step's leaf meets a busy vault
- **THEN** the request answers with its `progress` and with `stopped`, which names
  `MUTATION_BUSY`, that step and `committed: false`
- **AND** the phase is `executing` and `next_action` is `resume`
- **AND** the next resume commits that step once

#### Scenario: Another refusal stops a request after a committed step

- **WHEN** a request committed a step and the next step meets a refusal that is
  not a busy run or vault
- **THEN** the request answers with its `progress` and with `stopped`, which
  names that refusal's code, and raises no error
- **AND** the phase is `partial` or `blocked`, as the run's receipts show

### Requirement: A relocation derives one sealed plan from a set of pages

`curation.relocate` SHALL take `pages` or one `review_ref`, and optionally
`to_project`, `folder` and `from_project`. Without `to_project` it SHALL only
rename the pages to their titles. It SHALL derive every step on the server,
read-only, from registry attributes, and SHALL seal one plan for the whole set.
The memory refs among `pages` SHALL resolve over one identity census.

#### Scenario: Forty pages move to a new project in one approval

- **WHEN** the agent relocates 40 pages of two projects, among them an entity and
  an insight, to a new project, and the user approves the one preview
- **THEN** every compiled page moves to its title-named path in the new project's
  folder, and the entity stays in its own folder, takes its title-named path
  there and changes its project field
- **AND** each page keeps its identity and records its old path in `moved_from`
- **AND** no page links to an old path
- **AND** each cited Source differs only in its project fields and in its
  rewritten back-references

#### Scenario: A relocation without a project renames only

- **WHEN** the agent relocates two pages without `to_project`
- **THEN** each page moves to its title-named path in its own folder
- **AND** no project field of a page or a Source changes

#### Scenario: The whole relocation is undone with one approval

- **WHEN** the owner approves the compensation of a completed relocation
- **THEN** every moved page, edited Source and rewritten referrer returns to its
  bytes before the relocation

### Requirement: A relocation that runs short of time goes on from its prepared steps

A relocation SHALL prepare its steps under the request budget, by the rule that
stops execution between steps. When it stops before the seal, it SHALL keep the
steps it prepared, seal nothing, and answer `preparing` with `progress` and
`next_action: relocate`. The same relocation made again by the same audience
SHALL go on from the kept steps, and SHALL seal the plan that one uninterrupted
relocation would seal. The seal SHALL check every kept step's binding against
the vault.

#### Scenario: A long preparation seals over several calls

- **WHEN** preparing a relocation's steps takes longer than one request budget
- **THEN** each call answers `preparing` with its progress, and no call prepares
  a kept step again
- **AND** a later call seals the plan with the fingerprint that one uninterrupted
  relocation gives

#### Scenario: A page that changes between calls refuses the seal

- **WHEN** a page changes after its step was prepared and kept
- **THEN** the seal refuses with `CURATION_BINDING_STALE`, and the kept steps are
  dropped
- **AND** the same relocation made again prepares from the vault as it is, and
  seals

### Requirement: A relocation lists what it cannot change and seals the rest

A page that the lane cannot change SHALL be listed under `refused` with its
code, and the other pages SHALL still seal. A cited Source whose correction its
leaf refuses at preparation SHALL be listed under `refused` and keep its keys,
and the relocation SHALL go on without it.

#### Scenario: A Source that its leaf refuses is listed

- **WHEN** the leaf refuses the correction of one cited Source at preparation
- **THEN** the relocation seals, the Source is listed under `refused` with that
  code, and the other Sources gain the new key

### Requirement: A relocation plan holds one step per change

A derived relocation plan SHALL hold:

- a `move` with a `fields` patch for each page whose path changes;
- a pointer `edit` for each page whose path stays and whose fields change;
- an `edit` of the project fields of each Source that a relocated page cites;
- the `repoint-links` steps, last.

#### Scenario: A page whose path stays is corrected in place

- **WHEN** a relocation changes the project of a page whose type has no project folder
- **THEN** the plan edits that page's project fields and does not move it

### Requirement: A relocated page lands where the writer would put it

A compiled page's destination SHALL follow its note type's folder and the
project registry as the plan leaves it. Its filename SHALL follow its title
under the vault's filename style. A name that a file, a moved page's lineage,
an earlier move of the plan or another live page's title holds SHALL take the
writer's number, so the page never takes over another page's bare links.

#### Scenario: Another page holds the title

- **WHEN** a relocated page's title is the title of another live page
- **THEN** the page moves to its title's name with the writer's number

### Requirement: A plan may split its link rewrite by referring page

A `repoint-links` step MAY take `referrers`, the referring pages it rewrites. A
step without `referrers` SHALL rewrite every page that links to a page an
earlier `move` of the plan moves. A plan's `repoint-links` steps SHALL be its
last steps and SHALL follow a `move` step. A plan with more than one such step
SHALL give each step `referrers`, and no referring page SHALL be named by two
steps.

#### Scenario: Two link rewrites name one page

- **WHEN** an agent-written plan names one referring page in two
  `repoint-links` steps
- **THEN** proposal refuses the plan with `CURATION_STEP_INVALID`

### Requirement: A relocation with many referring pages rewrites them in bounded steps

A relocation SHALL derive one `repoint-links` step when its referring pages fit
one step's bound. Otherwise it SHALL derive steps of at most that bound, which
together name every referring page once. The bound SHALL keep one step's hold
of the vault mutation boundary well inside the request budget. Each step SHALL
seal only the moves that its pages use, and its compensation SHALL return only
the pages that step rewrote.

#### Scenario: A relocation with many referring pages

- **WHEN** a relocation moves pages that more referring pages link to than one
  step's bound
- **THEN** the plan ends with several `repoint-links` steps, each naming at most
  that bound of referring pages, and each referring page is named once
- **AND** each step seals only the moves whose pages its referring pages link to
- **AND** the compensation holds one link restore for each of those steps, and
  restores every referring page to its bytes before the relocation

### Requirement: A relocation keeps every key that another page still needs

A cited Source SHALL gain the destination key. It SHALL lose the replaced key
only when no citer outside the set still carries that key. A citer that links
the Source by one of its earlier paths SHALL count. A key that the plan
registers SHALL stay registered after the plan's compensation, and the
relocation's summary SHALL say that the plan registers it.

#### Scenario: A Source shared with a page that stays keeps the old key

- **WHEN** a relocated page and a page outside the set both cite one Source
- **THEN** the Source gains the new key and keeps the old key

#### Scenario: A page cites a Source by its earlier path

- **WHEN** a Source moved before, and a page outside the set that carries the
  old key still cites the Source's earlier path
- **THEN** the Source keeps the old key

### Requirement: A new project's folder is one portable name

`folder` SHALL be one portable folder name, the name the vault's writer would
give it, and the registry SHALL record it as a quoted scalar. Any other
`folder` SHALL be refused with `INVALID_RELOCATION`, and nothing SHALL be
written.

#### Scenario: A new project's folder is named

- **WHEN** the agent relocates pages to a new key with `folder`
- **THEN** the pages move into that folder and the registry records it for the key

#### Scenario: A folder that is not one portable name

- **WHEN** the agent relocates pages to a new key with a `folder` that holds a
  path separator or a colon
- **THEN** the relocation refuses with `INVALID_RELOCATION` and the registry
  keeps its bytes

### Requirement: A derived plan is bounded by its sealed size

A plan that a server action derives SHALL carry the sealed field `derived_by`,
whose closed values are the deriving actions. Such a plan SHALL take up to 2048
steps. An agent-written plan SHALL keep its 64-step and 256 KiB bounds. One
1 MiB bound SHALL apply to every stored run artifact, when the store writes it
and when any read loads it. A plan above that bound SHALL be refused at proposal
with `CURATION_PLAN_TOO_LARGE`, and nothing SHALL be written.

#### Scenario: An oversized relocation is refused before it seals

- **WHEN** a relocation would seal a plan above the stored-artifact bound
- **THEN** the relocation refuses with `CURATION_PLAN_TOO_LARGE`
- **AND** no run directory is created

#### Scenario: An agent-written plan keeps its step bound

- **WHEN** an agent-written plan holds 65 steps
- **THEN** proposal refuses it with `CURATION_PLAN_TOO_LARGE`

### Requirement: A step seals a preimage only when its undo reads one

A `move`, pointer `edit`, `repoint-links`, `delete` or `recover` step SHALL seal
no full-text preimage, because its compensation reads the sealed line delta,
the pointer-write record or the trash identity instead.

#### Scenario: A relocation's steps seal no page text

- **WHEN** a relocation seals its plan
- **THEN** no step of the plan holds a page's full text

### Requirement: A derived plan previews as a grouped summary

The relocation reply, `preview` and `propose-compensation` of a derived plan
SHALL answer with a grouped summary: the count of each group and at most 20
sample rows of it. The relocation reply SHALL group moves, pages corrected in
place, Sources gaining, losing and keeping a key, referrers rewritten, unchanged
pages and refused pages. The plan SHALL seal that summary, and a later
`preview` SHALL answer the same groups. None of these replies SHALL hold the
step list or the binding manifest.

#### Scenario: The person approves a summary

- **WHEN** the agent previews an 81-step relocation
- **THEN** the preview holds the summary, the binding health and the fingerprint
- **AND** the summary holds the groups that the relocation reply held
- **AND** it holds no step list and no binding manifest

#### Scenario: The undo of a relocation is summarized

- **WHEN** the owner proposes the compensation of a completed relocation
- **THEN** the reply holds the summary of the undo plan, not the plan

### Requirement: Execution of a derived run answers with counts

`apply` and `resume` of a derived run SHALL answer with counts and SHALL NOT
list every receipt. `status` SHALL still read the whole run.

#### Scenario: A derived run's apply lists no receipts

- **WHEN** the owner applies a derived relocation plan
- **THEN** the reply holds the phase and `progress`, and no receipt list
