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

### Requirement: Maps responses carry a catalogue of skills and collections

Unless `EXOMEM_ACTIVATION_SKILLS=off`, each maps response SHALL carry `skills` with `state`, `generation`, `items` and `omitted`.
Each item SHALL carry `kind` (`skill` or `collection`), `name`, a `summary` and a `ref` that opens its page.
Skill items SHALL list visible skill pages; collection items SHALL list visible Records collections by manifest title.
A summary SHALL be the first sentence of the description or manifest body, cut at a whole word to `skill_summary_max_chars`.

#### Scenario: A collection is listed beside the skills

- **WHEN** a vault holds two visible skills and one visible Records collection
- **THEN** the catalogue lists the two skills and then the collection, each with its kind, name, summary and ref

#### Scenario: Opening an item gives its full text

- **WHEN** the agent reads an item's `ref`
- **THEN** it receives the whole skill page or collection manifest, as a native skill load would

### Requirement: The catalogue is session context with vault-owned bounds

The catalogue SHALL count against its own bound, `skills_max_chars`, and SHALL NOT reduce the response's `max_chars` allowance.
`skills_max_chars` and `skill_summary_max_chars` SHALL be vault-owned `budgets`, defaults 3500 and 200, whole numbers in 0..8000 and 40..1000.
State SHALL be `ready` for at least one item, `empty` for none, `unchanged` when the caller holds this generation, and `unavailable` when compilation fails, which SHALL leave the map, stance and turn context available.

#### Scenario: A vault sets its own catalogue bounds

- **WHEN** a vault's activation-conventions override sets `budgets.skills_max_chars` and `budgets.skill_summary_max_chars`
- **THEN** the catalogue fits within that size and cuts each summary at that length, and a vault with no override keeps 3500 and 200

#### Scenario: An invalid bound keeps the shipped value

- **WHEN** an override sets a bound that is not a whole number inside its range
- **THEN** the shipped value applies and `generation.conventions_findings` names the rejected bound

#### Scenario: The first turn of a conversation receives the catalogue

- **WHEN** a caller with no continuity token activates context in a vault with three visible skills
- **THEN** `skills.state` is `ready` and the block lists three items in name order

#### Scenario: A later turn does not repeat an unchanged catalogue

- **WHEN** the caller's continuity token records the current catalogue generation
- **THEN** `skills.state` is `unchanged`, the block carries the generation and no items, and it spends no catalogue allowance

#### Scenario: An edited skill reaches the next turn

- **WHEN** a skill's description changes after the caller received the catalogue
- **THEN** the next response carries a new generation and the full current catalogue

### Requirement: The catalogue's listing does not depend on the turn

Whole items SHALL fit within `skills_max_chars`: skills before collections, each in name order.
The listing, `omitted` and `generation` SHALL NOT depend on the turn.

#### Scenario: A catalogue that fits ignores the turn

- **WHEN** every visible item fits within `skills_max_chars`
- **THEN** the catalogue lists them all in fit order, whatever the turn touches

### Requirement: An overflowing catalogue still names every item it could not list

When not every item fits, `names` SHALL give each unlisted item's name under its kind, in listing order, within `skills_names_max_chars` (default 2400, range 0..8000; 0 turns it off).
`omitted` SHALL count only the items neither listed nor named. `names` SHALL be part of `generation` and SHALL NOT depend on the turn.
The block SHALL carry `names_advice`, which says how to open a named collection and find a named skill on the serving surface.

#### Scenario: A collection shares no word or domain with the turn
- **WHEN** the catalogue overflows and a visible collection declares no topic and shares no word with the turn
- **THEN** `names` still gives that collection, so the agent can choose it

#### Scenario: A hidden collection under overflow
- **WHEN** the catalogue overflows and the caller cannot see a collection
- **THEN** `names` does not give that collection

### Requirement: An overflowing catalogue names the turn's unlisted items beside it

When not every item fits, `turn_items` SHALL name unlisted items that fit within `skills_turn_max_chars`: first those whose topics resolve through the domain registry to a domain the turn touches or its ancestor, then those the turn's task context reached.
`turn_items` SHALL stay outside `generation` and SHALL be sent on every turn that has them, also when the listing is `unchanged`.

#### Scenario: An overflowing catalogue names the touched domain's unlisted item

- **WHEN** the items do not all fit and an unlisted item belongs to the one registered domain the turn touches
- **THEN** the listing stays in fit order, `omitted` counts the items neither listed nor named, and `turn_items` names that item

#### Scenario: A held overflowing catalogue stays unchanged as the turn's domains change

- **WHEN** a caller holding an overflowing catalogue sends turns that touch different domains
- **THEN** the listing comes back `unchanged`, `next_action` does not point at it again, and only `turn_items` follow the turn

### Requirement: The catalogue tells the agent how to use it

When the catalogue lists items, `advice` SHALL say in one sentence that an item covering the turn is opened with the read tool and followed within existing authority.
The advice and every agent-facing sentence about the catalogue SHALL call a skill page a skill and a Records collection a collection.
Delivery SHALL grant no authority: the agent decides whether an item applies.

#### Scenario: A client without hooks learns to open a covering skill

- **WHEN** a tool-only client receives a catalogue that lists items
- **THEN** the block's `advice` tells it to open a covering item with the read tool before acting

### Requirement: The catalogue's turn points the agent at it

On the turn that receives listed items, `next_action` SHALL be `skill_check`, pointing the agent at the catalogue, unless a reset, a review-due stance or a selected item takes precedence.

#### Scenario: The catalogue's turn points at the catalogue

- **WHEN** a response lists catalogue items and no item was selected for the turn
- **THEN** `next_action.state` is `skill_check`, and a later turn that holds the catalogue is not pointed at it again

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

### Requirement: A bounded judgment selects the methods a turn needs

With a method-fit placement, task context SHALL offer a bounded model the visible skill pages and Records collection manifests, each labelled by name and described by its author's text: a skill's frontmatter `description`, a manifest's body.
The model SHALL see the turn, recent excerpts and the focus, and SHALL answer only through a schema whose items are the supplied labels.
The product SHALL hold no word list, threshold or list of methods for this selection.

#### Scenario: An ordinary turn selects the matching skill without naming it

- **WHEN** a turn asks for work that one visible skill covers without naming the skill, the product or any skill vocabulary, and the judgment selects it
- **THEN** `task_context.methods` leads with that skill, with `selection: "method_fit"` and its contract

#### Scenario: A judgment of none selects nothing

- **WHEN** the judgment answers with no label
- **THEN** no method item carries `selection: "method_fit"` and `method_fit.state` is `ready`

### Requirement: The method judgment stays within its bounds

The judgment SHALL select at most the vault's `method_fit_max_items` (shipped default 2, accepted 0..6; 0 turns selection off).
Skills SHALL be offered before collections. Carriers that would exceed the placement's input allowance SHALL be counted as `omitted`, not sent.
The judgment, reading its carriers and calling the model, SHALL take at most half of the request's time budget, so a door that its client abandons early still answers in time.

#### Scenario: A slow carrier read stops at the deadline

- **WHEN** reading the carriers would outlast half of the request's time budget
- **THEN** the judgment stops at that deadline, calls no model and reports `unavailable`

### Requirement: The method placement follows the interpretation rules

The placement SHALL be deployment configuration outside the vault (`EXOMEM_METHOD_FIT_CONFIG`), separate from the lead-ordering placement.
A local placement SHALL be a loopback Ollama endpoint. An API placement SHALL require explicit disclosure authorization and a provider audience.
An API placement SHALL offer only pages its provider audience may see. It SHALL read each description from the caller/provider intersection, with withheld references redacted. A hidden page SHALL change no label.

#### Scenario: A provider sees only what its audience may see

- **WHEN** an API placement's provider audience may not see a page that a skill description or a collection manifest links to
- **THEN** the provider input carries neither that page's path nor its text, and labels are unchanged by withheld pages

#### Scenario: A provider never sees a withheld method's label

- **WHEN** an API placement's provider audience may not see a skill page or a collection manifest
- **THEN** the provider input carries neither that method's label nor its description

### Requirement: Selected methods are served first and grant no authority

Each selected page SHALL be served first in `task_context.methods`, whole, with `selection: "method_fit"`, `kind` `skill` or `collection`, its `name`, its description, a `read_ref` and its contract. A selected page SHALL NOT also be served as a plain lead.
Selection SHALL grant no authority: the agent decides whether the method applies and acts only within its existing authority.

#### Scenario: A selected skill is not repeated as a lead

- **WHEN** the judgment selects a skill that general retrieval also ranked as a lead
- **THEN** the skill appears once, as the selected method

### Requirement: The method judgment reports its state

`task_context.method_fit` SHALL report `disabled` without a placement, `empty` when nothing could be offered, `ready` with the model identity after a judgment, and `unavailable` when the call fails, times out or answers outside the schema.
Every state except `ready` SHALL select nothing and leave the rest of the response unchanged.

#### Scenario: A failed judgment keeps the turn

- **WHEN** the model call times out or names a label it was not offered
- **THEN** no method is selected, `method_fit.state` is `unavailable`, and the leads and other methods are served as without a placement

#### Scenario: No placement, no judgment

- **WHEN** no method-fit placement is configured
- **THEN** no model is called and `method_fit.state` is `disabled`

### Requirement: A selected method carries its contract

A method the judgment selected SHALL carry `contract` with `text` and `complete`.
The text SHALL be the page body as released to the caller, cut at a line boundary to at most the vault's `method_contract_max_chars` (shipped default 2400, accepted 0..8000; 0 omits the contract).
`complete` SHALL be true only when the text is the whole released body.
The contract SHALL grant no authority and SHALL be rendered as quoted page text.

#### Scenario: A short skill arrives whole

- **WHEN** a selected skill's released body fits within `method_contract_max_chars`
- **THEN** its method item carries the whole body with `complete: true`

#### Scenario: A long skill arrives cut with a read handle

- **WHEN** a selected skill's released body exceeds `method_contract_max_chars`
- **THEN** its method item carries the leading lines with `complete: false` and the `read_ref` to load the rest

### Requirement: A method contract fits the turn allowance

The rendered line SHALL tell the agent to follow the contract where it fits when `complete` is true, and to read the page for the rest when it is false.
The contract SHALL count against the turn-context allowance. A contract that does not fit SHALL be cut further, never dropped with its method. A method that does not fit even with an empty contract SHALL be omitted and reported as `budget`.

#### Scenario: Two selected methods share a tight allowance

- **WHEN** two selected methods' contracts together exceed the turn-context allowance
- **THEN** both methods are served and their contracts are cut, rather than the second method being dropped

### Requirement: Skills follow page admission and egress

Every skills catalogue item and projected skill method SHALL pass the same admission and terminal egress checks as other served page material.
A skill the caller cannot see SHALL affect no item, count or generation in that caller's response.

#### Scenario: A withheld skill is invisible

- **WHEN** a restricted caller cannot see one of three skill pages
- **THEN** its catalogue lists two skills, `omitted` is 0 and its generation equals that of a vault holding only the two visible skills
