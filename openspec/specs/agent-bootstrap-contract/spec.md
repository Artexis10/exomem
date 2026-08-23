# agent-bootstrap-contract Specification

## Purpose
Give a generic agent without a native Exomem skill a deterministic, versioned
operating contract instead of guessing conventions: a read-only `bootstrap`
operation that returns workflow guidance, tool defaults, performance profiles,
and search guidance as structured JSON, without inspecting or summarizing any
private vault content.

## Requirements

### Requirement: Agent Bootstrap Contract
The system SHALL expose a read-only `bootstrap` operation that returns a versioned operating contract for agents using Exomem without a native skill. The contract MUST be deterministic, structured JSON and MUST NOT inspect or summarize private vault content. Its entity-capture section SHALL list the active vault-aware entity type IDs and explain that unknown recurring types are registered only through the governed save leaf with a rationale.

#### Scenario: Compact bootstrap returns the operating contract
- **WHEN** `bootstrap` is called with default arguments
- **THEN** the response includes `contract_version`, `server`, `workflow`, `tool_defaults`, `performance_profiles`, `search_guidance`, and `common_tools`
- **AND** the response identifies the current compute policy
- **AND** the response does not include note bodies, excerpts, paths from the user's vault contents, or private project names

#### Scenario: Entity capture exposes active extension types
- **WHEN** a vault defines active entity type `place` and calls bootstrap
- **THEN** `entity_capture.types` contains `place` and every core type
- **AND** its guidance routes unknown recurring types to `save-entity-types` with `why`

#### Scenario: Invalid bootstrap profile is rejected
- **WHEN** `bootstrap(profile="invalid")` is called
- **THEN** the operation fails with a validation error naming the accepted profiles

### Requirement: Bootstrap Profiles
The system SHALL support `compact`, `full`, and `diagnostics` bootstrap profiles.
`compact` SHALL be the default. `full` SHALL include concrete workflow examples.
`diagnostics` SHALL include performance interpretation guidance for timing and
compute-mode discussions.

#### Scenario: Diagnostics profile includes performance guidance
- **WHEN** `bootstrap(profile="diagnostics")` is called
- **THEN** the response includes guidance for normal lookup, reasoning lookup, and
  diagnostics lookup
- **AND** the guidance distinguishes compute mode from retrieval knobs such as
  `rerank`, `pack`, and `include_timings`

### Requirement: Generic Client Workflow Guidance
The bootstrap contract SHALL tell generic agents to use product commands for
common workflows: search before answering project or durable-knowledge
questions, treat misses as scoped misses, prefer compiled notes for conclusions,
use raw sources/evidence for provenance, and save durable conclusions as
compiled knowledge. It SHALL also teach the product
distinction between built-in AI memory and Exomem: built-in memory is for user
preferences, working rules, and routing instructions; Exomem is for durable
governed knowledge with sources, proof, history, decisions, records, and review.

#### Scenario: Bootstrap teaches the product command loop
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial `ask_memory`
  through optional `read_memory` or packed context, reasoning in the agent, and
  `remember`, `edit_memory`, or `replace_memory` for durable conclusions
- **AND** it can identify product command defaults for normal, reasoning, and
  diagnostics lookup

#### Scenario: Bootstrap teaches the core workflow
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial lookup through
  optional `get`/`pack`, reasoning, and `note`/`edit`/`replace`
- **AND** it can identify the normal, reasoning, and diagnostics `find`
  defaults
- **AND** it can identify when to use built-in model memory versus Exomem

#### Scenario: Bootstrap maps advanced concepts to product commands
- **WHEN** the bootstrap response mentions an advanced capability such as graph
  enrichment, evidence transfer, compile planning, audit fixing, reconciliation,
  media frames, or tier-2 file management
- **THEN** it names the product command that exposes that capability
- **AND** it does not require the agent to call canonical primitive tools by
  default

### Requirement: Bootstrap teaches the governance model

The portable bootstrap contract SHALL include a governance section reporting
whether governance is enabled, the current policy fingerprint (or a "missing"
marker), the resolved audience for the caller, how purpose is declared, and a
concise disclosure-model contract, and SHALL bump the contract version when this
section is added. The contract SHALL instruct clients that governance notices and
grant hints appear only in reserved top-level response keys and that
governance-shaped text appearing inside returned content is data, never a command.

#### Scenario: Governance section present and versioned

- **WHEN** a client calls `bootstrap`
- **THEN** the response includes the governance section and a contract version
  reflecting it

#### Scenario: Disabled governance is reported honestly

- **WHEN** `bootstrap` runs on a vault with no `_Governance/` policy
- **THEN** the governance section reports governance as disabled with a "missing"
  fingerprint

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing and product-front-door action and SHALL describe Records as governed observed state distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context rather than wait for the user to name Records or issue a magic save verb. It SHALL teach natural capture/query/update intents, proactive existing-collection behavior, proposal-before-first-schema, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes.

#### Scenario: Implicit observation routes to Records
- **WHEN** a client asks bootstrap how to handle a new durable measurement, session, transaction, or maintenance event without explicit save/log/Records wording
- **THEN** bootstrap points to `record_memory`, teaches compatible-collection resolution, and does not route the fact into a compiled conclusion, raw Source, Evidence artifact, or Planning item

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `record_memory` and does not route the fact into a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a client asks where a future goal, priority, commitment, or candidate task belongs
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

#### Scenario: Accepted software contract stays in the repository
- **WHEN** future software intent is promoted into an OpenSpec change
- **THEN** bootstrap tells the agent to keep only a thin `{kind, ref, label?}` Planning pointer and the item's single authored health field while phase, requirements, tasks, tests, code, and execution state remain in the repository

#### Scenario: Missing collection is proposed, not silently activated
- **WHEN** observed state fits Records but inventory contains no compatible collection
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal and forbids silent schema creation

### Requirement: Bootstrap exposes collection guidance compactly

Bootstrap SHALL expose all finite Records actions and a bounded product-facing collection-authoring summary. Compact bootstrap SHALL identify `_collection.md`, supported Records collection versions and profiles, the `describe -> validate -> create -> inspect -> append` authoring workflow, and the `validate -> revise` plus `inspect -> rebaseline` maintenance workflows. It SHALL route clients to `record_memory(action="describe")` for the complete technical contract and SHALL NOT embed the full manifest JSON Schema, parser field table, or worked manifest. It SHALL teach intent before storage vocabulary and SHALL NOT imply that guidance activates a collection or migration.

#### Scenario: Compact bootstrap routes without exposing parser internals
- **WHEN** a generic client calls compact bootstrap
- **THEN** it can identify the exact route for agent-facing manifest discovery and read-only validation
- **AND** the payload does not contain the complete JSON Schema or complete manifest example

#### Scenario: Bootstrap guidance is not mutation
- **WHEN** bootstrap includes Records authoring guidance
- **THEN** no collection, folder, template, migration, or canonical data is activated merely by reading bootstrap

#### Scenario: Pack guidance is not mutation
- **WHEN** bootstrap includes health or personal-records Records guidance
- **THEN** the payload marks it as guidance and no collection, folder, template, migration, or canonical data is activated merely by reading bootstrap

#### Scenario: Template remains optional
- **WHEN** a collection recommends an ordinary Markdown template
- **THEN** bootstrap explains that humans may insert or edit it directly and that schema validation does not depend on the template file

### Requirement: Documentation Aligns With Bootstrap Contract
Agent-facing documentation SHALL align generic-client onboarding with the
existing `bootstrap()` operating contract.

#### Scenario: Generic client instructions match bootstrap behavior
- **WHEN** a generic MCP client, hosted chat client, or client without the
  Exomem skill is documented
- **THEN** the guidance tells the agent to call `bootstrap(profile="compact")`
  once at session start
- **AND** uses the existing bootstrap action model rather than inventing a
  conflicting memory workflow

#### Scenario: Diagnostics remain separate from normal lookup
- **WHEN** documentation discusses performance or retrieval behavior
- **THEN** it distinguishes normal lookup from diagnostics and does not imply
  that rerank, packed context, cold model load, or compute mode are the same
  concern

### Requirement: Bootstrap Presents Simple Actions First
The bootstrap contract SHALL present simple product actions before the full technical tool catalog, so generic agents can route common user intents without learning every command.

#### Scenario: Compact bootstrap includes action routing
- **WHEN** `bootstrap(profile="compact")` is called
- **THEN** the response includes a simple action catalog with action names, intent descriptions, default canonical routes, and safety notes
- **AND** the response still avoids note bodies, private vault paths, and private project names

#### Scenario: Bootstrap distinguishes simple and advanced tools
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the normal route for ask, remember, capture, review, connect, adopt, and maintain
- **AND** it can identify when to fall through to advanced canonical commands

### Requirement: Scaffold Guidance Uses Intent Language
The installed Exomem skill and operation references SHALL teach agents simple user-intent language before listing canonical tools.

#### Scenario: Agent maps user words to actions
- **WHEN** a generic agent reads the scaffolded skill or operation reference
- **THEN** it sees examples such as "ask what I know", "remember this conclusion", "capture this source", "review stale knowledge", "connect this note", and "adopt this vault"
- **AND** each example names the canonical operation route to use

### Requirement: Bootstrap Advertises Only Active Surface Capabilities

Every bootstrap response SHALL be built against an immutable descriptor for the invoking surface and active Tier-2 policy. Every named tool in routes, defaults, examples, catalogs, advanced guidance, and `common_tools` MUST be present in that surface's actual exported command set.

#### Scenario: MCP runs with Tier 2 disabled
- **WHEN** bootstrap is called over an MCP server whose Tier-2 tools are disabled
- **THEN** no Tier-2 command is recommended or listed as available
- **AND** every remaining bootstrap tool reference exists in live `tools/list`

#### Scenario: REST and CLI bootstrap differ from MCP
- **WHEN** bootstrap is called through REST or CLI
- **THEN** MCP-only commands are omitted from that response
- **AND** every advertised tool maps to an actual REST/OpenAPI operation or CLI command respectively

### Requirement: Canonical And Active Surface Identity Are Distinct

Bootstrap SHALL label the packaged canonical MCP discovery fingerprint separately from the active surface descriptor. It MUST NOT present the canonical full-surface fingerprint as proof that a filtered deployment exports those tools.

#### Scenario: Filtered deployment reports capabilities
- **WHEN** active command names differ from the packaged canonical MCP surface
- **THEN** bootstrap reports the active surface name, Tier-2 policy, and command names
- **AND** retains the canonical fingerprint only under an explicitly canonical label

### Requirement: Bootstrap Profiles Conform To Their Exported Surface

A conformance test SHALL inspect `compact`, `full`, and `diagnostics` bootstrap profiles for MCP, REST, CLI, and hosted surfaces and compare all tool references with the respective exported schemas.

#### Scenario: Bootstrap gains a new recommendation
- **WHEN** a tool name is added to any profile or workflow example
- **THEN** the conformance test fails unless that tool is exported on every surface where the recommendation appears or the response filters it out

### Requirement: Every Bootstrap Profile Carries Minimum Authoring Semantics

The `compact`, `full`, and `diagnostics` bootstrap profiles SHALL each include the
same versioned minimum semantic-authoring object. It SHALL contain the exact
compact syntax, canonical `## Observations` section, open category versus governed
kind distinction, one-valid-unit minimum for new active compiled notes, compact
versus rich choice and rich heading boundary, preferred typed write routes, Tier-2 applicability,
stable refusal codes, and remediation. Expanded profiles MAY add examples but
SHALL NOT weaken or contradict the minimum object.

#### Scenario: Default bootstrap is enough to write correctly
- **WHEN** a generic client calls `bootstrap()` with the default compact profile
- **THEN** the response alone explains how to author a valid active compiled note, default to compact `[category]` form, and choose a non-empty rich unit without duplicating content

#### Scenario: Profiles agree on normative fields
- **WHEN** compact, full, and diagnostics bootstrap responses are compared
- **THEN** their semantic-authoring contract version and normalized normative fields are identical

#### Scenario: Bootstrap remains content-blind
- **WHEN** bootstrap returns semantic-authoring guidance for a populated vault
- **THEN** the guidance is built without reading note bodies and contains no vault-derived example, path, project key, or identifier

### Requirement: Bootstrap Teaches Portable Semantic Authoring

The bootstrap contract SHALL project the versioned portable category vocabulary and explain that category is one primary role-or-domain lens, kind is the governed form, tags are secondary facets, and relations are typed links. Compact bootstrap MUST include the core keys, the open-category escape hatch, and one compact example. Full bootstrap MUST add a generic rich example and category-selection guidance. Neither profile may inspect private vault content.

#### Scenario: Compact bootstrap is sufficient to author a reusable unit

- **WHEN** a generic agent calls `bootstrap(profile="compact")`
- **THEN** the response includes the canonical core category keys and one parseable compact unit
- **AND** the response states that an intentional unknown category remains valid

#### Scenario: Full bootstrap teaches rich relations

- **WHEN** a generic agent calls `bootstrap(profile="full")`
- **THEN** the response includes a generic rich semantic block with a stable identifier and typed relation
- **AND** no private path, project name, or vault-derived vocabulary appears

### Requirement: Executable Reviewed-None Bootstrap Guidance

The bootstrap contract SHALL describe the reviewed-none creation handshake using the exact
canonical value `reviewed_none` and the exact public parameter names. It MUST tell callers
to validate first, use the returned `relation_review_hash`, supply an explicit bounded
reason, and commit the unchanged draft. It MUST NOT refer to a response field that is not
present.

#### Scenario: Generic agent can commit a zero-candidate draft

- **WHEN** an agent reads bootstrap guidance and validation reports that reviewed-none is
  required
- **THEN** the guidance supplies `relation_disposition="reviewed_none"`
- **AND** it tells the agent to echo the returned `relation_review_hash`
- **AND** it requires an explicit `relation_review_reason`
- **AND** no undocumented guess is needed to form the commit call

### Requirement: Bootstrap exposes Planning actions and guidance compactly
Bootstrap SHALL expose the six finite Planning actions, the four Planning kinds, lifecycle/priority/commitment/horizon vocabulary, manual-first Markdown ownership, default inbox capture, opaque Records evidence and execution-pointer boundaries, and relevant selected-pack guidance in a bounded product-facing shape. It SHALL teach intent before collection vocabulary and SHALL NOT imply that reading guidance creates a collection, performs Review, synchronizes an external system, or activates a pack blueprint.

#### Scenario: Agent can file an idea without internal vocabulary
- **WHEN** a generic client asks how to remember a possible future feature
- **THEN** bootstrap teaches candidate inbox capture through `plan_memory(action="add")` without requiring the user to choose schema, adapter, marker, or folder internals

#### Scenario: Multi-horizon question routes to structured query
- **WHEN** a user asks what is planned this week, quarter, year, or over multiple years
- **THEN** bootstrap routes to a bounded Planning saved view/query, labels horizons as explicitly maintained buckets rather than inferred calendar truth, and uses date filters when the question requires exact calendar bounds

#### Scenario: Pack guidance creates nothing
- **WHEN** bootstrap includes technical, business, health, personal, or creative Planning guidance
- **THEN** the payload marks it as guidance and no collection, item, template, hierarchy, or migration is activated merely by reading bootstrap

#### Scenario: No command-alias workflow skill is required
- **WHEN** an agent has the generic Exomem skill or bootstrap contract
- **THEN** ordinary Planning capture, triage, and query are fully routable without installing a separate skill that only repeats `plan_memory` arguments

### Requirement: The agent contract teaches structural-promotion presentation

Bootstrap and the canonical write loop SHALL teach agents that a compiled write may return one advisory `structure_suggestion`, and SHALL state how to act on it.

The guidance SHALL direct agents to normally surface a `strong` suggestion, to describe it in the user's own domain language rather than in Exomem's internal terms, to ask before reorganising unless the user has explicitly delegated curation, to prefer an existing suitable destination over creating a new one, to avoid repeating the same recommendation within one interaction, and to exercise judgement on a `moderate` suggestion where mentioning it would be bureaucracy rather than help.

The guidance SHALL state that the suggestion is advisory and that the runtime never reorganises knowledge on its own.

#### Scenario: Bootstrap names the signal and the expected behaviour

- **WHEN** a generic client reads the bootstrap post-write guidance
- **THEN** it learns that a durable write may return a structural suggestion and where to find it
- **AND** it learns to surface a strong suggestion, to ask before restructuring, and to prefer an existing destination

#### Scenario: Guidance is presentational, not executable

- **WHEN** bootstrap describes structural suggestions
- **THEN** reading bootstrap creates, moves, or reorganises nothing
- **AND** no new tool or parameter is required to receive the suggestion

### Requirement: Post-write guidance names only fields the response carries

The write-loop guidance SHALL describe the fields a successful mutation actually returns at its default response detail, and SHALL identify the response detail required for any field it names that is not returned by default.

#### Scenario: The write loop does not direct agents to absent fields

- **WHEN** the canonical write loop tells an agent to inspect the result of a durable write
- **THEN** every field it names is either present in the default committed response
- **AND** or is explicitly marked as requiring a higher response detail

### Requirement: Bootstrap teaches the epistemic commitments

The portable bootstrap contract SHALL include a dedicated section stating the
commitments that govern how durable knowledge may change over time, expressed as
imperative instructions rather than description.

The section SHALL state that raw captured material is append-only and MUST NOT be
rewritten or deleted, that a changed conclusion SHALL be superseded rather than
overwritten so the earlier view stays readable, that a durable expectation about a
future observation SHALL be written down before its answer is known, that a judgment
SHALL be categorical and MUST NOT be recorded as a number, percentage, or hedge, and
that a refuted claim SHALL keep active standing rather than being treated as
superseded.

The section SHALL state that a genuine conflict between conclusions is recorded as a
typed relation and MUST NOT be silently reconciled away.

#### Scenario: The contract states the commitments imperatively

- **WHEN** a generic client reads the bootstrap contract
- **THEN** it can identify the append-only rule for raw material, the supersession rule
  for changed conclusions, the rule that an expectation is recorded before its outcome,
  the prohibition on numeric confidence, and the rule that a refuted claim stays active
- **AND** it can identify that a genuine conflict is recorded as a typed relation

#### Scenario: Reading the contract changes nothing

- **WHEN** bootstrap returns the epistemic commitments
- **THEN** no page, relation, unit, folder, or migration is created by reading them
- **AND** the response contains no vault content, path, or private project name

### Requirement: Bootstrap teaches the shipped epistemic vocabulary

The portable bootstrap contract SHALL name the governed vocabulary an agent needs to
close a claim: the closed set of epistemic outcomes, the governed unit-metadata keys
that carry a judgment and a revisit date, and the governed unit kinds for a question, a
hypothesis, and a prediction.

The taught outcome set SHALL be exactly the closed set the runtime accepts, and the
taught governed unit-metadata keys SHALL be exactly the keys the runtime parses and
validates. The contract MUST NOT teach an outcome, key, or kind the runtime does not
accept.

The contract SHALL state that the revisit date is one exact ISO calendar date, that it
is a due date rather than an expiry, and that nothing is removed, decayed, or
downranked when it passes.

#### Scenario: The taught vocabulary matches the runtime vocabulary

- **WHEN** a client reads the bootstrap epistemic vocabulary
- **THEN** the outcomes it names are exactly the outcomes the runtime accepts for a
  verdict
- **AND** the governed unit-metadata keys it names are exactly the keys the runtime
  parses
- **AND** the unit kinds it names for a question, a hypothesis, and a prediction are
  governed kinds the runtime recognises

#### Scenario: The revisit date is taught as a due date

- **WHEN** a client reads the guidance for the revisit-date key
- **THEN** it learns the exact calendar-date format
- **AND** it learns that passing the date removes, expires, or downranks nothing

### Requirement: Bootstrap nudges durable expectations into predictions

The portable bootstrap contract SHALL instruct agents that a durable expectation about a
future observation is recorded as a prediction unit carrying a revisit date, rather than
left in prose, left in the assistant's own short-term memory, or recorded as an observed
fact.

The contract's routing guidance SHALL distinguish a claim about a future observation
from observed state and from planning intent, so an expectation is not misrouted into
either.

#### Scenario: An expectation becomes a prediction

- **WHEN** an agent reads the capture guidance and holds a durable expectation about a
  future observation
- **THEN** it learns to record it as a prediction unit with a revisit date
- **AND** it learns that the assistant's own short-term memory is not the place for it

#### Scenario: An expectation is not an observation or a plan

- **WHEN** the contract describes the boundary between observed state and intended
  future state
- **THEN** it also identifies a checkable claim about a future observation as neither

### Requirement: Bootstrap exposes question, hypothesis, and prediction recipes

The bootstrap authoring recipes SHALL include a recipe for recording an open question, a
hypothesis, and a prediction. Each recipe SHALL state that it describes material
authored inside a compiled page and MUST NOT imply that it is a new page type.

The prediction recipe SHALL name the revisit-date key and the verdict key, and SHALL
state that correcting the wording of a prediction preserves its verdict.

#### Scenario: Recipes are present and correctly scoped

- **WHEN** a client reads the bootstrap authoring recipes
- **THEN** it finds a recipe for a question, a hypothesis, and a prediction
- **AND** each states that it is authored inside a compiled page rather than as its own
  page type

### Requirement: The epistemic contract reaches every client tier

The epistemic commitments and vocabulary SHALL be present in the default compact
bootstrap profile, not only in richer profiles.

The commitments SHALL remain present when the active surface exports a reduced set of
commands. Guidance naming a specific command MAY be removed for a surface that cannot
call it, but removing such guidance MUST NOT remove a commitment.

The contract version SHALL move when this section is added.

#### Scenario: Compact carries the doctrine

- **WHEN** a generic client calls bootstrap with default arguments
- **THEN** the response includes the epistemic commitments and the epistemic vocabulary

#### Scenario: A reduced surface keeps the commitments

- **WHEN** bootstrap runs on an active surface that exports almost no commands
- **THEN** the epistemic commitments are still present in full
- **AND** the response names no command that surface cannot call

### Requirement: Bootstrap teaches that source classification vocabularies are open

Bootstrap SHALL tell a generic agent that source kind and subject domain are open, extensible vocabularies rather than closed enumerations, that a meaningful new label is accepted without a release, and that project association is a separate multi-valued axis.

Where bootstrap names example labels it SHALL frame them as non-exhaustive defaults, so an agent does not read the shipped set as the permitted set. Example labels SHALL be generic and SHALL NOT include any user-specific project, client, or organisation identifier.

Bootstrap SHALL NOT publish the fallback kind as the default argument for capture.

#### Scenario: The contract states the vocabularies are open

- **WHEN** an agent reads the compact bootstrap contract
- **THEN** it learns that source kind and subject domain are open and extensible
- **AND** it learns that project association is separate and may name more than one project
- **AND** any labels it is shown are marked as non-exhaustive

#### Scenario: The fallback is not advertised as the capture default

- **WHEN** an agent reads the capture routing guidance in bootstrap
- **THEN** that guidance does not instruct it to pass the fallback kind

#### Scenario: Published example labels carry no user-specific identifier

- **WHEN** the bootstrap payload is inspected for classification guidance
- **THEN** every example kind, domain, and project label is generic

### Requirement: Bootstrap teaches how to treat the fallback and a classification suggestion

Bootstrap SHALL tell an agent to classify a source semantically when it can, to use the fallback kind only when classification genuinely cannot be determined, and specifically not to use the fallback merely because no built-in label matches.

Bootstrap SHALL tell an agent to inspect an advisory classification suggestion returned after a capture, to surface a strong one in the user's own domain language rather than in product-internal terms, and to exercise judgement on a weaker one rather than repeating advice.

This guidance SHALL fit within the existing compact-profile size budget.

#### Scenario: The contract distinguishes low confidence from missing vocabulary

- **WHEN** an agent reads the source-capture guidance
- **THEN** it learns to name the kind it believes is correct even when that label is unfamiliar to the product
- **AND** it learns that the fallback means low confidence, not absent vocabulary

#### Scenario: The contract teaches suggestion handling

- **WHEN** an agent reads the post-write guidance
- **THEN** it learns to inspect a returned classification suggestion
- **AND** it learns to present a strong one in domain language
- **AND** it learns not to repeat the same advice within one interaction

#### Scenario: Compact guidance stays within budget

- **WHEN** the compact bootstrap payload is produced with this guidance present
- **THEN** its serialized size remains within the established compact ceiling

### Requirement: Bootstrap exposes configured classification vocabulary without becoming a second ontology

Bootstrap SHALL be able to surface the classification labels a selected knowledge pack makes discoverable, so a configured agent sees relevant vocabulary immediately.

Pack-surfaced labels SHALL be advisory discovery hints that resolve against the same source-taxonomy vocabulary. A pack SHALL NOT define a competing classification model, and selecting a pack SHALL NOT create, reserve, or require any label.

Classification SHALL function fully with no pack selected.

#### Scenario: A selected pack surfaces relevant labels

- **WHEN** a pack is selected and bootstrap is requested at a profile that carries pack detail
- **THEN** the payload can present that pack's suggested kinds and domains
- **AND** those labels resolve against the same source-taxonomy vocabulary

#### Scenario: Pack selection creates nothing

- **WHEN** a pack that suggests classification labels is selected
- **THEN** no registry entry, directory, or reservation is created for those labels

#### Scenario: Classification works with no pack selected

- **WHEN** no pack has been selected
- **THEN** source kind and domain classification, projection, and retrieval all still function

### Requirement: The capture predicate covers an executed method whose outcome is reported

Every surface that teaches an agent when to capture SHALL name an executed method with a reported outcome as a stepping stone, alongside the durable conclusion and the recurring entity.

The condition SHALL be stated so that it holds independently of domain: a concrete method was actually carried out, the user reports the result, the result is clearly positive, negative, or diagnostically informative, and the method or the lesson it yields is reusable later. It SHALL apply whether the outcome was a success, a failure, or only the bounding of a parameter.

The guidance SHALL state where the material goes rather than leaving one page type to absorb the class: a proven method to its own compiled page, a parameter exploration or comparison to an experiment, a diagnosed failure mode to a failure note. An episode that yielded nothing reusable SHALL remain unwritten.

The guidance SHALL state that an explicit request to save, arriving after the result has already landed, is the failure being corrected and not the trigger to wait for.

#### Scenario: The bootstrap payload teaches the class to a client with no skill

- **WHEN** any client requests the bootstrap contract
- **THEN** the payload's epistemic contract names the executed-method-with-reported-outcome case
- **AND** it states the routing by what the outcome yielded
- **AND** it states that waiting to be asked is the failure

#### Scenario: The engagement contract names it at the levels that capture

- **WHEN** the engagement contract is resolved at `balanced` or at `maximal`
- **THEN** its capture clause names the executed-method case alongside the durable conclusion and the recurring entity
- **AND** the levels that do not capture on the agent's own judgment are unchanged

#### Scenario: A hookless client's pasted instructions carry the same class

- **WHEN** the documented copy-paste instruction blocks are used on a client with no hooks and no skill
- **THEN** the capture line names the executed-method case
- **AND** each block remains within its documented character cap

#### Scenario: An episode with nothing reusable is still not captured

- **WHEN** a method was carried out but yields no reusable method, comparison, boundary, or diagnosis
- **THEN** the guidance directs that nothing is written

### Requirement: Bootstrap Presents Simple Front-Door Actions
The bootstrap contract SHALL present the primary user/agent actions as save,
adopt/import, ask, prove, review, update, and connect. For each action it SHALL
name the preferred tool or composition of tools, the internal typed operation(s)
that enforce governance, and any selected-pack routing guidance. It SHALL keep
advanced tools visible but secondary.

#### Scenario: Bootstrap exposes available and selected packs
- **WHEN** an agent reads the bootstrap response
- **THEN** it can list available built-in packs with beginner descriptions
- **AND** it can list selected packs and their agent instructions
- **AND** a missing selection falls back to a default personal-records pack

#### Scenario: Agent can route a proof request
- **WHEN** an agent reads the bootstrap response and the user asks "prove this"
  or "save this for my warranty case"
- **THEN** the agent can identify the evidence/proof workflow
- **AND** it can distinguish that workflow from ordinary source capture
- **AND** selected pack guidance can refine the route without exposing internal
  ontology to the user

#### Scenario: Agent can route an existing-vault request
- **WHEN** an agent reads the bootstrap response and the user asks to import or
  adopt an old vault
- **THEN** the agent can identify the scan-first adoption workflow
- **AND** it knows existing files are read-only by default

### Requirement: Records routing is salient in compact bootstrap

Compact bootstrap SHALL serialize beginner/front-door actions and the bounded Records route before the large semantic-authoring projection. The first byte of the `record` route SHALL occur before byte 8,192 of the compact payload, and the complete compact payload SHALL be no larger than 57,344 UTF-8 bytes. Full parser/schema detail SHALL remain opt-in through `record_memory(action="describe")`.

#### Scenario: Record route appears before semantic authoring detail
- **WHEN** compact bootstrap is serialized with the full active MCP product surface
- **THEN** the `record` action and Records intent boundary appear before semantic-authoring detail and within the tested early-position budget

#### Scenario: Compact budget rejects salience regression
- **WHEN** unrelated bootstrap material grows enough to push Records beyond its byte-position or total-size budget
- **THEN** the bootstrap contract test fails even though a Records key still exists somewhere in the payload

### Requirement: Bootstrap teaches referent abstention
Bootstrap and the shipped scaffold SHALL teach agents to name only resolved entities, report the unresolved remainder for partial results, ask on ambiguity, and never guess on unresolved results.

#### Scenario: Partial referent result
- **WHEN** an agent receives partial with unresolved_count one
- **THEN** it names the resolved entity and says one identity remains unresolved
