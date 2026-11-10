## MODIFIED Requirements

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
- **THEN** it can identify the recommended loop from initial `search_memory`
  through optional `read_page` or packed context, reasoning in the agent, and
  `create_note`, `edit_note`, or `supersede_note` for durable conclusions
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

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing and product-front-door action and SHALL describe Records as governed observed state distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context rather than wait for the user to name Records or issue a magic save verb. It SHALL teach natural capture/query/update intents, proactive existing-collection behavior, proposal-before-first-schema, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes. It SHALL teach that Planning can operate standalone or coordinate with any companion under a resolved user-authored workflow contract; no named companion SHALL be a product-wide default. It SHALL teach the `records_routing` advisory: when a committed note or Evidence write names a collection, route the observation into it through `add_record` under the served capture disposition, resuming a held candidate when one exists, and never append from the advisory alone without the observation; that a collection may declare `claims.match`; that a disposition on the advisory carries its own instruction; and that a grouped backfill item is asked about once at every prominence and never filed without a yes. This teaching SHALL stay command-free so reduced surfaces keep it. It SHALL name the `collection_candidate` category and teach that a strong candidate is proposed in domain language with a schema drafted through `describe` and `validate`, that creating the collection requires one inline confirmation, and that backfill uses only exactly dated units cited in `sources`.

#### Scenario: Implicit observation routes to Records
- **WHEN** a client asks bootstrap how to handle a new durable measurement, session, transaction, or maintenance event without explicit save/log/Records wording
- **THEN** bootstrap points to `add_record`, teaches compatible-collection resolution, and does not route the fact into a compiled conclusion, raw Source, Evidence artifact, or Planning item

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `add_record` and does not route the fact into a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a client asks where a future goal, priority, commitment, or candidate task belongs
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

#### Scenario: Accepted software contract stays in the repository
- **WHEN** future software intent is promoted into an OpenSpec change
- **THEN** bootstrap tells the agent to keep only a thin `{kind, ref, label?}` Planning pointer and the item's single authored health field while phase, requirements, tasks, tests, code, and execution state remain in the repository

#### Scenario: Companion execution artifacts stay with their declared owner
- **WHEN** future intent is promoted into an artifact whose type a resolved workflow contract assigns to a companion tool
- **THEN** bootstrap tells the agent to keep only an opaque Planning execution reference and connective context while the declared artifact contents and execution state remain with the companion

#### Scenario: Standalone is the absence-safe default
- **WHEN** no active workflow contract applies
- **THEN** bootstrap teaches that Planning remains fully functional standalone and does not require or infer an external tool

#### Scenario: Missing collection is proposed, not silently activated
- **WHEN** observed state fits Records but inventory contains no compatible collection
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal and forbids silent schema creation

#### Scenario: A routing advisory is acted on in domain language
- **WHEN** a client asks bootstrap what to do with a `records_routing` advisory on a committed Evidence write
- **THEN** bootstrap tells it to append the observation to the named collection under the served capture disposition, to resume a held candidate if one exists, and never to append from the advisory alone

#### Scenario: A candidate becomes a collection only after one confirmation
- **WHEN** a client asks bootstrap what to do with a strong `collection_candidate`
- **THEN** bootstrap tells it to draft the schema through `describe` and `validate`, ask one question in domain language, create only on confirmation, and backfill only exactly dated units cited in `sources`

#### Scenario: Compact bootstrap stays within its byte ceiling
- **WHEN** the compact profile is measured after these clauses are added
- **THEN** it does not exceed the pinned ceiling, with the trimmed text and both measured sizes recorded

#### Scenario: Bootstrap points at the disposition without naming commands
- **WHEN** a client reads the compact bootstrap's routing teaching
- **THEN** it names `claims.match`, says the disposition carries its own instruction, says a grouped backfill is asked about once at any prominence, and names no command, within the compact byte ceiling

### Requirement: Bootstrap exposes collection guidance compactly

Bootstrap SHALL expose all finite Records actions and a bounded product-facing collection-authoring summary. Compact bootstrap SHALL identify `_collection.md`, supported Records collection versions and profiles, the `describe -> validate -> create -> inspect -> append` authoring workflow, and the `validate -> revise` plus `inspect -> rebaseline` maintenance workflows. It SHALL route clients to `inspect_memory(operation="records.describe")` for the complete technical contract and SHALL NOT embed the full manifest JSON Schema, parser field table, or worked manifest. It SHALL teach intent before storage vocabulary and SHALL NOT imply that guidance activates a collection or migration.

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

### Requirement: Bootstrap exposes Planning actions and guidance compactly
Bootstrap SHALL expose the six finite Planning actions, the four Planning kinds, lifecycle/priority/commitment/horizon vocabulary, manual-first Markdown ownership, default inbox capture, opaque Records evidence and execution-pointer boundaries, and relevant selected-pack guidance in a bounded product-facing shape. It SHALL teach intent before collection vocabulary and SHALL NOT imply that reading guidance creates a collection, performs Review, synchronizes an external system, or activates a pack blueprint.

#### Scenario: Agent can file an idea without internal vocabulary
- **WHEN** a generic client asks how to remember a possible future feature
- **THEN** bootstrap teaches candidate inbox capture through `add_plan_item` without requiring the user to choose schema, adapter, marker, or folder internals

#### Scenario: Multi-horizon question routes to structured query
- **WHEN** a user asks what is planned this week, quarter, year, or over multiple years
- **THEN** bootstrap routes to a bounded Planning saved view/query, labels horizons as explicitly maintained buckets rather than inferred calendar truth, and uses date filters when the question requires exact calendar bounds

#### Scenario: Pack guidance creates nothing
- **WHEN** bootstrap includes technical, business, health, personal, or creative Planning guidance
- **THEN** the payload marks it as guidance and no collection, item, template, hierarchy, or migration is activated merely by reading bootstrap

#### Scenario: No command-alias workflow skill is required
- **WHEN** an agent has the generic Exomem skill or bootstrap contract
- **THEN** ordinary Planning capture, triage, and query are fully routable without installing a separate skill that only repeats `add_plan_item` arguments

### Requirement: Records routing is salient in compact bootstrap

Compact bootstrap SHALL serialize beginner/front-door actions and the bounded Records route before the large semantic-authoring projection. The first byte of the `record` route SHALL occur before byte 8,192 of the compact payload, and the complete compact payload SHALL be no larger than 57,344 UTF-8 bytes. Full parser/schema detail SHALL remain opt-in through `inspect_memory(operation="records.describe")`.

#### Scenario: Record route appears before semantic authoring detail
- **WHEN** compact bootstrap is serialized with the full active MCP product surface
- **THEN** the `record` action and Records intent boundary appear before semantic-authoring detail and within the tested early-position budget

#### Scenario: Compact budget rejects salience regression
- **WHEN** unrelated bootstrap material grows enough to push Records beyond its byte-position or total-size budget
- **THEN** the bootstrap contract test fails even though a Records key still exists somewhere in the payload

### Requirement: The capture predicate covers stated intent and observed outcomes

The capture axis of the engagement contract at `balanced` and `maximal` SHALL name two lifecycle classes beside a durable conclusion, a recurring entity and a carried-out method: a *stated intent or commitment* — the user says what they will do, commits to a batch or workstream, sequences work, or re-prioritises — routes to Planning through `add_plan_item`; an *observed outcome or event* — the conversation reports that something happened, was produced, measured, delivered, approved, published or failed — routes to Records through `add_record`. The contract SHALL state the pairing rule: an observed outcome that lands on an open committed Planning item is one landing with two consequences, the Records append and then the Planning transition, performed together and reported once. It SHALL state that a tentative claim is never written as an event and that elapsed time is never an outcome. The existing Records engagement policy (exactly one compatible collection and a sufficiently identified observation → write and report; competing collections → one focused question; no collection → propose, never silently create) SHALL be the write gate for both classes. `light` SHALL keep capture-only-when-asked and `off` SHALL be unchanged. Both `SKILL.md` copies, `prominence.py` and the bootstrap `intent_boundary` and `capture_examples` SHALL carry the same rule, and the compact payload SHALL stay within its measured ceiling.

#### Scenario: A stated outcome closes a queued deliverable without a magic word

- **WHEN** a Planning collection keyed on title holds a queued committed work item and the user says in ordinary language that that deliverable was produced today, without mentioning Exomem, Planning, Records, save, track or remember
- **THEN** an agent under `balanced` or `maximal` appends the production event to the one compatible Records collection, transitions the work item out of its open state, and reports both in one line in the user's domain language

#### Scenario: Sequencing language files intent, not events

- **WHEN** the user says the remaining deliverables will be done next time
- **THEN** the agent leaves them queued in Planning, writes no Records event, and does not ask whether to "track" them

#### Scenario: A tentative claim is not an event

- **WHEN** the user says a deliverable was probably published but is not sure
- **THEN** the agent writes no publication event and, where the collection offers a note field, may record the stated uncertainty there; it never fabricates an observed event

#### Scenario: Light prominence does not widen

- **WHEN** the engagement level is `light`
- **THEN** neither lifecycle class triggers an unprompted write and the contract text for that level does not name them as proactive

### Requirement: Bootstrap exposes the Planning inventory and the plan simple action

Bootstrap SHALL teach that `query_plan_items` without a collection returns the Planning inventory, that an observation is resolved to a Planning item with `query` filtered on `title` or the natural-key fields plus `lifecycle` and `status`, and SHALL list `plan` among the simple front-door actions with the same phrasing `SKILL.md` uses. Reading the inventory SHALL create nothing.

#### Scenario: Fresh session finds the Planning surface

- **WHEN** a session starts with no collection named and the user refers to "the next one" in a workstream
- **THEN** bootstrap routes the agent to the Planning inventory and a bounded query rather than to a question about which collection to use

### Requirement: The client capture nudge recognises lifecycle writes

Where a client hook enforces the capture contract, its detector of knowledge-base writes SHALL count `add_record`, `add_plan_item` and `edit_note` mutations as writes, and its reminder SHALL name the lifecycle classes in one line. The hook SHALL remain structural: it decides whether a write happened, never what should have been written.

#### Scenario: A turn that filed the record is not nudged

- **WHEN** a turn's only knowledge-base mutation is a `add_record` append
- **THEN** the capture nudge does not fire

#### Scenario: The reminder names the classes

- **WHEN** the capture nudge fires
- **THEN** its reminder names stated intent → Planning and observed outcome → Records alongside the existing classes, in one line

### Requirement: Bootstrap exposes workflow contract resolution compactly

Every bootstrap profile SHALL carry the immutable workflow invariant kernel and built-in standalone fallback. A profile exporting `inspect_memory` or `manage_memory` SHALL also carry at most one released default summary, at most eight released scoped summaries in deterministic key order, exact released-total/projection-truncation metadata after a complete scan, and the shared schema/configuration route for contract inventory and resolution. A profile omitting the command SHALL advertise no route and report `resolution_available: false`; it SHALL expose built-in standalone only for an empty released inventory with no migration requirement, otherwise fixed `workflow_resolution_unavailable` with contract-aware proactive routing disabled. Bootstrap SHALL teach route-capable agents to resolve context at the start of substantial work or durable scoping, including explicit null for known-absent dimensions, then inspect relevant Planning before capture. It SHALL distinguish declared companion tools from capabilities actually exported by the active client. If the durable pre-feature marker requires review without an active released workflow contract, bootstrap SHALL report `workflow_contract_migration_required` and SHALL NOT silently select standalone.

#### Scenario: Generic client learns the personalized workflow
- **WHEN** a generic MCP client whose active profile exports `inspect_memory` starts against a vault with a scoped companion contract
- **THEN** bootstrap gives it enough information to resolve that contract before planning work without installing a private skill or receiving hard-coded companion instructions

#### Scenario: Contract inventory creates nothing
- **WHEN** bootstrap reads workflow summaries
- **THEN** no contract, Planning item, Record, external artifact, or integration state is created or changed

#### Scenario: Legacy workflow requires a choice
- **WHEN** an upgraded pre-feature vault has a review-required migration marker and no active released workflow contract
- **THEN** bootstrap reports a content-bounded migration requirement and a route-capable profile directs the agent to explicit session selection or reviewed contract save before ordinary fallback

#### Scenario: Profile without schema command remains honest
- **WHEN** a Planning-capable active profile omits `inspect_memory` and a released scoped contract exists
- **THEN** bootstrap reports `workflow_resolution_unavailable`, advertises no unavailable route, and disables contract-aware proactive routing rather than choosing a broader/default contract

#### Scenario: Hidden contract is absent from bootstrap
- **WHEN** the vault contains an unreleased workflow contract
- **THEN** bootstrap is byte-equivalent to the result for an otherwise identical vault without that contract

### Requirement: Generic guidance carries the activation line
At balanced and maximal prominence, the generic client workflow guidance served by
bootstrap SHALL include one line instructing the agent to call `recall_context`
with the user's turn before answering a substantive turn that has no prior
conversation context, and to resolve a returned `ambiguous` packet by calling again
with `anchor` set; at light and off it SHALL be absent. The line SHALL cost at most 220 bytes in the
served JSON and SHALL be byte-identical between the served projection and the shipped
scaffold's recall loop. With the line present the compact profile SHALL stay within its
byte ceiling at every prominence level and on every client surface, and SHALL keep at
least its 512-byte headroom warning margin at the default (balanced) level. The maximal
level was already inside that warning margin before this line existed, so the margin is
not required of it; its measured headroom SHALL be recorded beside the ceiling constant.

#### Scenario: Ceiling holds at every level, margin at the default
- **WHEN** the compact bootstrap is served at each of off, light, balanced and maximal,
  for the generic surface and for a hook-capable client surface
- **THEN** every projection is within the compact byte ceiling, and the balanced
  projections keep at least 512 bytes of headroom

#### Scenario: Line present at maximal, absent at light
- **WHEN** bootstrap is served for an identity whose effective level is maximal and
  again for one whose level is light
- **THEN** the first guidance contains the activation line and the second does not,
  and the compact profile stays within its ceiling in both cases

### Requirement: The client episode ask needs a landing since the last ask or record

Where a client hook asks for an episode record, it SHALL ask only when work has
landed (by the same detector as the capture nudge) since the last episode ask
or, if newer, the last successful `save_episode` record, in addition to its
turn-count and cooldown cadence. A landing seen before the cadence is due SHALL
be remembered until it is. A session that never lands work SHALL NOT be asked,
and an ask answered without a record SHALL NOT be repeated until a new landing.
The `stop_hook_active` self-disarm, the revision re-base against the service,
candidate-coverage asks, `prominence=off` and the cadence overrides SHALL be
unaffected.

#### Scenario: A session that never lands work is not asked

- **WHEN** a session at `balanced` completes many substantive turns whose commands never commit, push, merge, tag or create a pull request or release
- **THEN** the episode ask does not fire

#### Scenario: A landing makes a due ask fire

- **WHEN** the turn count and cooldown are satisfied and a turn then runs `git push` successfully, in a Claude or a Codex transcript
- **THEN** the episode ask fires on that Stop

#### Scenario: An unanswered ask waits for a new landing

- **WHEN** an episode ask fired and the agent made no record, and later turns satisfy the cooldown without a landing
- **THEN** the ask does not repeat until a turn lands work

#### Scenario: A record resets the pending landing

- **WHEN** a successful `save_episode` record is made after a landing
- **THEN** the ask needs both a fresh landing and a fresh turn count

### Requirement: Stop-hook blocks are one short paragraph

Where a client Stop hook blocks to ask for a capture or an episode record, the reason SHALL be one short paragraph, asserted by a byte ceiling per block. The capture check SHALL be the same short text on every fire, with no first-fire or post-compaction full text, and SHALL NOT carry the capture doctrine. It SHALL name the rules that prevent known incidents (the live-policy pointer, no transcripts, `supersede_note` supersedes a contradicted conclusion, stated intent to Planning and observed outcome to Records, transient code, test and CI output stays out, and nothing durable means stop).

The capture check SHALL end with a pointer to the installed Exomem skill or compact bootstrap when the skill is unavailable. The agent SHALL reuse an already-held current operating contract. The pointer SHALL NOT require a vault copy of the shipped references.

The episode ask SHALL keep the `save_episode` record call, the episode key and its do-nothing escape. When the hook runs against the native MCP connection, its preamble SHALL be one clause of at most 90 bytes that says to skip when Exomem is not connected or cannot capture and to bootstrap first if the contract is absent. Shortening a block SHALL NOT change when it fires.

#### Scenario: Every capture fire is the short check

- **WHEN** the capture check fires three times in one session, or fires after a compaction
- **THEN** each fire carries the same short check

#### Scenario: A block is short in MCP mode

- **WHEN** the capture check or the episode ask fires against the native MCP connection
- **THEN** the whole reason, preamble included, is within its byte ceiling and the preamble is at most 90 bytes

#### Scenario: The pointer reaches the rules

- **WHEN** an agent with only the MCP connection follows the capture check's pointer
- **THEN** compact bootstrap returns the portable operating contract without a vault reference read

#### Scenario: A current contract remains reusable

- **WHEN** the agent already holds the current operating contract or can read the installed Exomem skill
- **THEN** the capture check does not require another bootstrap or a vault reference read

### Requirement: Bootstrap Teaches The Semantic Unit Language
The bootstrap contract SHALL explain compact observation syntax, rich semantic blocks, the distinction between open category and governed kind, exact category/kind recall, page/unit structured filters, filter-only retrieval, opt-in ranking explanations, canonical typed relations, logically atomic and crash-recoverable validate-then-commit relation review for disconnected creation, and review-before-governance behavior. It SHALL tell agents to use structured semantic-unit mutation when changing one unit instead of brittle whole-page string surgery, that compact observations cannot carry typed unit relations without selecting a rich governed kind, and that BM25/cosine/fusion/reranker values have different meanings rather than being confidence scores.

#### Scenario: Generic agent can author and retrieve observations
- **WHEN** a generic agent reads the full bootstrap contract
- **THEN** it can author `- [category] content` observations, distinguish categories from tags and kinds, query exact categories, combine safe page/unit filters, request score explanations when needed, and use `edit_note` for unit mutation

#### Scenario: Bootstrap does not teach automatic semantics
- **WHEN** bootstrap describes open categories and relation suggestions
- **THEN** it states that categories do not imply governed kinds and suggestions require semantic review before typed authoring

### Requirement: Compact bootstrap names the route to the next due batch

The compact core's due-state summary SHALL report the per-category visible and outstanding counts and one `next` route, `list_review_items(queue="due", categories=[<category with the most visible items>], limit=<batch>)`. The route SHALL be built from the category name, with no per-category table. The engagement guidance SHALL teach the batch loop: list a batch, group it by topic, act through the existing writers, and check that the outstanding count fell; and it SHALL state that a dismissal is not completion. The summary SHALL stay within the compact bootstrap byte ceiling.

#### Scenario: A session sees how to start on the backlog

- **WHEN** a compact bootstrap runs on a vault with outstanding `unprocessed_source` items as its largest visible category
- **THEN** the due-state summary carries the per-category counts and a `next` route naming `unprocessed_source`

### Requirement: The agent contract teaches the note-cohesion question

Bootstrap and the canonical write loop SHALL teach agents that a compiled write may return one `structure_suggestion` with `kind` `note_cohesion`, and SHALL state how to answer it.

The guidance SHALL direct the agent to read the whole page and name the independent questions it answers. It SHALL keep a page that answers one question whole, however long, by dismissing the item through `decide_review_item` with an `intentional:` reason. It SHALL propose a split only when several questions each have their own material, and only through the curation lane with the user's exact plan confirmation. It SHALL add a breadth tag through a governed edit when the page is a hub by design. The guidance SHALL state that the question asserts nothing about the page and that the runtime never reorganises knowledge on its own.

#### Scenario: Bootstrap names the question and the three answers

- **WHEN** a generic client reads the bootstrap post-write guidance
- **THEN** it learns that a compiled write may carry a note-cohesion question and where to find it
- **AND** it learns to keep a coherent page whole by dismissal, to split only through the curation lane, and to mark a hub with a breadth tag

#### Scenario: Guidance is presentational, not executable

- **WHEN** bootstrap describes the note-cohesion question
- **THEN** reading bootstrap creates, moves, or reorganises nothing
- **AND** no new tool or parameter is required to receive or answer the question

### Requirement: A session closes by recording the recap and citing it

At a decision or stopping point, the shipped skill, engagement reference, Stop-hook episode ask and `episode_due` advisory SHALL tell the agent to record one recap, then compile or explicitly dismiss every Decided item before citing it. The agent SHALL update each item's canonical page with the ordinary writers (`edit_note`, `add_observation`, `supersede_note`). Only after the whole pass is complete SHALL the agent cite the recap from updated compiled notes. Entity, plan and record pages take no citation. The guidance SHALL explain that one compiled note's citation holds every line in that recap revision, so citation linkage is not semantic coverage. The Stop-hook episode ask SHALL stay one short line within its established byte ceilings and SHALL make capture conditional on live policy. At `off`, or when the user asked only for a recap, a recap request alone SHALL NOT authorize page edits. The `save_episode` tool description SHALL state the completion-before-citation condition and SHALL point to the `compile` block. The existing rules SHALL hold: record only when the user asks or the live `proactive_capture` disposition permits it, record nothing when nothing durable happened, and treat a hook reminder as no permission. No text SHALL ask the agent to record the same recap a second time, and none SHALL ask the agent to link a decided item to a page.

#### Scenario: The close step reaches a client with no hook

- **WHEN** a tool-only client reads the `save_episode` tool description
- **THEN** the description tells the agent to finish or dismiss every Decided item before citing the recap from updated compiled notes
- **AND** it points to the `compile` block of the response

#### Scenario: The Stop hook asks for the citation only under live policy

- **WHEN** the Stop hook fires its episode ask
- **THEN** the ask puts the citation step after the condition that live policy permits capture
- **AND** it ends with "Otherwise do nothing."
