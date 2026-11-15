## Purpose

Present the Agent Skills a vault holds to every client the way native clients present installed skills: a short catalogue in context, the full skill loaded on demand.

## ADDED Requirements

### Requirement: A skill page is recognized by the Agent Skills format

A page SHALL be a skill when its file name is `SKILL.md` and its frontmatter carries a non-empty string `name` and a non-empty string `description`.
Pages under Sources or Evidence SHALL NOT be skills, because captured material is not an active instruction.
Recognition SHALL depend only on this format. The product SHALL hold no list of skill names, locations or kinds.
Other files in a skill's directory SHALL remain ordinary pages.

#### Scenario: A skill directory anywhere in managed knowledge is recognized

- **WHEN** a page `Knowledge Base/Guides/release-notes/SKILL.md` has `name: release-notes` and a description
- **THEN** the context catalogue marks it as a skill and stores its frontmatter description as its description

#### Scenario: A malformed skill page is an ordinary page

- **WHEN** a `SKILL.md` page has no `description` in its frontmatter
- **THEN** it is not a skill and appears only as an ordinary page

#### Scenario: Captured skill text is not a skill

- **WHEN** a Source page named `SKILL.md` carries a name and a description
- **THEN** it is not a skill

### Requirement: Maps responses carry a turn-independent skills catalogue

Unless `EXOMEM_ACTIVATION_SKILLS=off`, each maps response SHALL carry `skills` with `state`, `generation`, `items` and `omitted`.
Each item SHALL carry the skill's `name`, a `summary` and a `ref` that opens the skill page.
The summary SHALL be the first sentence of the description, cut at a whole word to at most the vault's `skill_summary_max_chars`.
Items SHALL follow skill name order. Whole items SHALL fit within the vault's `skills_max_chars`; `omitted` SHALL count visible skills that did not fit.
Both bounds SHALL be vault-owned `budgets` in the activation conventions, with shipped defaults of 3500 and 200, accepted as whole numbers in 0..8000 and 40..1000.
The catalogue SHALL be session context: it SHALL count against its own bound and SHALL NOT reduce the response's `max_chars` allowance for turn context.
State SHALL be `ready` for at least one item, `empty` for no visible skill, `unchanged` when the caller already holds this generation, and `unavailable` when compilation fails.
A compilation failure SHALL leave the map, stance and turn context available.

#### Scenario: A vault sets its own skills bounds

- **WHEN** a vault's activation-conventions override sets `budgets.skills_max_chars` and `budgets.skill_summary_max_chars`
- **THEN** the skills block fits within that block size and cuts each summary at that length, and a vault with no override keeps 3500 and 200

#### Scenario: An invalid bound keeps the shipped value

- **WHEN** an override sets a bound that is not a whole number inside its range
- **THEN** the shipped value applies and `generation.conventions_findings` names the rejected bound

#### Scenario: The first turn of a conversation receives the catalogue

- **WHEN** a caller with no continuity token activates context in a vault with three visible skills
- **THEN** `skills.state` is `ready` and the block lists three items in name order

#### Scenario: A later turn does not repeat an unchanged catalogue

- **WHEN** the caller's continuity token records the current skills generation
- **THEN** `skills.state` is `unchanged`, the block carries the generation and no items, and it spends no catalogue allowance

#### Scenario: An edited skill reaches the next turn

- **WHEN** a skill's description changes after the caller received the catalogue
- **THEN** the next response carries a new generation and the full current catalogue

### Requirement: A caller can omit the skills it loads natively

A request MAY name skills the client already loads natively in `native_skills`, a list of skill names.
The catalogue SHALL omit visible skills whose `name` equals a listed name, and SHALL count them in neither `items` nor `omitted`.
Native omission SHALL NOT change task discovery, which may still project an omitted skill as a relevant method.

#### Scenario: A client with native copies receives only the rest

- **WHEN** a vault holds skills `a`, `b` and `c` and the request names `native_skills: ["a", "b"]`
- **THEN** the catalogue lists only `c`

### Requirement: A relevant skill is projected as a method

When task discovery admits a skill page as a candidate, the response SHALL project it as a method item that carries the skill's `name`, its full `description`, `kind: "skill"` and a `read_ref` to the skill page.
The projected text SHALL count against the turn-context allowance like other method items.
Delivery SHALL grant no authority: the agent reads the skill and decides whether it applies.

#### Scenario: A matching task surfaces the skill and its trigger text

- **WHEN** a turn asks for work that a visible skill's description covers and task discovery admits that skill page
- **THEN** `task_context.methods` contains the skill with its full description and the read reference

### Requirement: Opening a skill page lists its other files

When `read_page` returns a skill page, the response SHALL list the caller-visible files in the skill's directory and its subdirectories, other than the skill page, as vault-relative paths in path order.
Each listed path SHALL be readable with `read_page`. Hidden files SHALL be neither listed nor counted.
The listing SHALL cost a bounded read: it examines a fixed maximum number of pages and lists a smaller maximum, and when either bound cuts it the response SHALL mark the list partial instead of counting pages it did not examine.

#### Scenario: A skill's references are one read away

- **WHEN** an agent opens a skill page whose directory holds `references/style.md`
- **THEN** the response lists `references/style.md` with its vault-relative path

#### Scenario: A skill above a large tree

- **WHEN** an agent opens a skill page whose directory holds more pages than the listing examines
- **THEN** the read examines at most the bound, lists at most the listing maximum and marks the list partial

### Requirement: Skills follow page admission and egress

Every skills catalogue item and projected skill method SHALL pass the same admission and terminal egress checks as other served page material.
A skill the caller cannot see SHALL affect no item, count or generation in that caller's response.

#### Scenario: A withheld skill is invisible

- **WHEN** a restricted caller cannot see one of three skill pages
- **THEN** its catalogue lists two skills, `omitted` is 0 and its generation equals that of a vault holding only the two visible skills
