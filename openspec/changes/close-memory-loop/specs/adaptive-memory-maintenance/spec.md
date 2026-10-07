## Purpose

Keep working context and knowledge organisation useful as the vault evolves through governed corrections, derived profiles and bounded consolidation proposals.

## ADDED Requirements

### Requirement: Adaptation is evidence-bound and governed

Corrections and observed capture/activation misses SHALL produce reviewable evidence-bound candidates for registered roles, cues, aliases, conventions or other supported definitions. The active agent SHALL decide semantics under the applicable typed writer and authority, preserving the current context-role owner-authored override gate. Accepted changes SHALL be versioned, reversible and visible to fresh sessions. Product code SHALL NOT hardcode private entities, suppliers, domains or task phrases. Adaptation SHALL NOT change identity, provenance, authority or abstention invariants, or infer an unregistered authority action from an additive grant.

The sensor for an activation correction SHALL be the agent's admitted `anchor` pick, classified from structure the request already holds and never parsed from the turn's language. A counted miss SHALL record a path, a class, a count and times, and SHALL NOT persist any word of the turn. At most one bounded advisory SHALL ride on the pick's response, after the guard admitted the choice; it SHALL name only existing typed writers and the hash guards they require, SHALL write nothing and grant nothing, SHALL NOT be cached or keyed, and SHALL honour the proactive-capture envelope, family quiet and off, per-item dismissal and snooze by fingerprint, and a per-target cooldown. A learned name SHALL live in the anchor page's `learned_aliases` and SHALL change activation only — never link resolution, egress name matching or identity. A learned referential cue or filler word SHALL live in the vault's activation conventions and SHALL change turn analysis only.

#### Scenario: A correction improves the next session

- **WHEN** an authorized correction revises an applicable context role or vault convention
- **THEN** a fresh session consumes its current version and the previous version remains attributable and recoverable
- **AND** the same operation without appropriate authority cannot silently edit the definition

#### Scenario: A correction teaches a name the next session uses

- **WHEN** a turn's words reach no anchor and the agent picks the page the user meant
- **AND** the agent adds the user's word to that page's `learned_aliases` through `edit_memory` with the advisory's page hash
- **THEN** a fresh session's turn containing the word resolves the page on `exact_alias`
- **AND** removing the entry restores the earlier abstention

#### Scenario: A learned name respects existing visible name claims

- **WHEN** `edit_memory` saves a `learned_aliases` spelling admitted by the activation index that another visible page already claims
- **THEN** the existing claimed-name guard refuses it unless the write carries a matching explicit distinct-identity decision
- **AND** admission and advice use the persisted YAML field and value, not their request spelling or scalar type
- **AND** an entry the index skips remains warning-only and does not claim a name
- **AND** a withheld claimant has the same effect as an absent one
- **AND** learned names remain activation-only and do not enter link resolution or identity

#### Scenario: A cue is learned in another language

- **WHEN** the agent saves a referential cue in the user's language through `schema_memory save-conventions` with the advisory's conventions hash
- **THEN** a fresh session's turn made only of that cue resolves the hot referent on recency
- **AND** no anchor sidecar is rebuilt and no continuity token is stranded
- **AND** a turn that speaks the cue and names an anchor resolves the named anchor

#### Scenario: Turn filler does not redefine learned names

- **WHEN** a referential filler override changes
- **THEN** learned-name admission continues to use the shipped filler safety floor
  and effective stopwords, consistently in writer warnings and index builds
- **AND** an unrelated later rebuild does not change a learned alias solely because
  the turn filler override changed

#### Scenario: A stale advisory is refused

- **WHEN** the page or the conventions changed after the advisory was issued
- **THEN** the write carrying the advisory's hash is refused by the writer's own guard

#### Scenario: Learning grants nothing

- **WHEN** a pick carries a learning advisory
- **THEN** no vault file changes, the review state is not stamped, and the packet cache holds no advisory

#### Scenario: Learning preserves the caller's released view

- **WHEN** a caller picks a released anchor while other anchors are withheld
- **THEN** learning classification and advisory content use only the caller's released
  heat, anchor vocabulary and page fields
- **AND** withheld anchors have the same effect as absent anchors
- **AND** an unsafe page read suppresses the advisory

#### Scenario: A dismissed advisory stays quiet until new evidence

- **WHEN** the agent dismisses an advisory by its ref and fingerprint
- **THEN** further misses in the same miss bucket carry no advisory
- **AND** a miss that moves the bucket carries a new one

### Requirement: Hot profiles remain bounded derived projections

The system SHALL provide a compact hot profile derived from authorized canonical knowledge with source provenance, explicit budgets and currency. Its sources SHALL be typed events recorded by origin where each act happens — governed work outside any batch, admitted picks, recorded episodes, reads, citations, and external edits judged by the write-burst rule — held in a bounded, machine-local, disposable ring, carrying the caller's attribution only as salted, audience-scoped derivations, and never re-derived from file times. Its budgets SHALL be declared: a bounded ring, a bounded external fold per request, a bounded seed, and at most a declared few page reads per request. It SHALL decay by displacement and by the working-session window, never by a clock. Corrections, expiry, deletion, supersession and access changes SHALL invalidate affected profile material. Missing or stale profiles SHALL report their state — `current`, `partial`, `seeded`, `behind` or `empty` — and SHALL NOT become an alternate canonical store or bypass disclosure checks: the profile is never served, every referent or entry drawn from it SHALL cross the reader's guard per request, and for a caller other than the owner it SHALL be ranked only over the pages released to that caller, so a withheld page and an absent one are indistinguishable in everything the caller receives.

#### Scenario: A profiled preference is corrected or hidden

- **WHEN** its canonical source changes or current access no longer permits disclosure
- **THEN** subsequent packets omit or refresh the affected profile entry and never serve the stale value as current

#### Scenario: A pick corrects the referent

- **WHEN** a referential turn resolved one anchor and the agent then picks another page for the same conversation
- **THEN** the next referential turn resolves the picked page, the pick being the latest deliberate act

#### Scenario: A session ends and its heat expires

- **WHEN** the latest act is older than the working-session gap and the user then reads a page
- **THEN** the older work no longer supplies the referent, the new session does, and nothing was removed by a clock

#### Scenario: A deleted or superseded page leaves the profile

- **WHEN** the hottest page is deleted, archived or superseded
- **THEN** it is never offered as a referent or a recent-context entry, and the next eligible page leads

#### Scenario: A page the caller may not see leaves no trace

- **WHEN** the page that leads the profile, or any page in it, is one the caller may not read
- **THEN** the caller's profile is ranked only over the pages released to it, so its referent, recent-context block and reported profile state and session start are exactly what they would be had that page never been touched, and no `withheld` abstention arises from activity it may not see
- **AND** a referent the turn itself names still abstains `withheld` when it may not be released

### Requirement: Priors cannot override grounded resolution

Activation priors SHALL be bounded derived ranking signals with provenance, versioning and invalidation. They SHALL NOT create facts, resolve ambiguous identity without evidence, override explicit task anchors or promote superseded state. Acceptance SHALL measure usefulness and latency alongside rare-anchor and popularity-trap negatives.

#### Scenario: Frequent history is irrelevant to the current task

- **WHEN** a high-frequency prior conflicts with a resolved explicit task anchor
- **THEN** grounded task relevance wins and irrelevant history cannot consume the entire context budget

#### Scenario: A page read many times is not hotter for it

- **WHEN** one page has been read many times and another was worked on once, more recently
- **THEN** the page worked on leads the referent, because the profile ranks the latest act and counts nothing, and repeated serving of a packet adds no heat

### Requirement: Dreamer proposes bounded consolidation off the interactive path

The dreamer SHALL provide deterministic, delta-driven or idle-scheduled consolidation proposals using bounded indexed evidence. Candidate families SHALL include supported alias/anchor, category/convention, link, hydration, profile and episode-recap fold improvements. For the profile and episode-recap fold families, a withheld recap, referrer or Source SHALL change nothing a restricted caller observes, delivery timing included, except the residuals design §8 accepts: which rows survive the row caps at saturation, and a withheld Source that merges origins. The active agent SHALL remain the semantic decider and canonical writers SHALL enforce current authority. Background execution SHALL be default-off and provide pause/quiet controls, explicit work/time/memory bounds, checkpointed continuation, evidence-version invalidation and deduplication. It SHALL NOT perform autonomous canonical writes or be required for online capture/recall.

Eligible proposals SHALL enter the existing bounded review/activation carrier at an ordinary supported lifecycle boundary without requiring an explicit review request. Delivery SHALL respect current quiet/defer settings and existing budgets. Tool-only clients SHALL expose the same proposals with best-effort initiation. Acceptance SHALL establish next-session delivery and authorized agent disposition, not only queue creation.

The background worker SHALL write only its own disposable sidecar. It SHALL NOT write the vault, take the writer lease or mutation guard, enqueue graph debt, mark freshness pending, build or repair an index, or load or run a model. It SHALL run only while the service is idle and the graph owes no work, SHALL yield to any request between pages, and SHALL take its changed pages from the freshness registry's delta or a diff against its persisted page signatures, never from a filesystem walk or a whole-vault snapshot. A caller SHALL receive at most one proposal per session start.

#### Scenario: An idle pass finds repeated disconnected knowledge

- **WHEN** a bounded pass finds eligible evidence for a connection or stale entity facet
- **THEN** it creates a provenance-bearing review candidate for agent adjudication without authoring a canonical edge or fact
- **AND** repeating the pass over unchanged evidence does not duplicate the candidate

#### Scenario: The next ordinary session receives consolidation work

- **WHEN** consolidation creates an eligible non-quieted candidate and a supported lifecycle boundary occurs
- **THEN** the next ordinary session receives it within the existing review/activation budget without a user review reminder
- **AND** the agent records a current evidence-bound disposition and applies any permitted effects only through canonical writers

#### Scenario: Consolidation is paused or fails

- **WHEN** background work is disabled, paused, unavailable or exceeds its resource budget
- **THEN** online capture and recall continue under their normal contracts and optional work remains resumable
- **AND** quieting proposals cannot hide non-quietable integrity failures

#### Scenario: The dreamer is off by default

- **WHEN** no operator setting enables it
- **THEN** no worker thread starts, no sidecar is created, and activation, recall and capture are byte-identical to a build without it

#### Scenario: Background work never creates write churn

- **WHEN** the worker processes pages during an ordinary session
- **THEN** it writes no vault file, takes no writer lease or mutation guard, raises no graph debt, marks no freshness pending, builds or repairs no index and loads no model
- **AND** it starts no tick while writes are landing, and a write burst's latency, graph availability and drain outcomes match a run with it off

#### Scenario: A restart resumes from recorded signatures without a walk

- **WHEN** the service restarts with pages changed while it was down
- **THEN** the worker finds them by diffing the live freshness map against its recorded signatures and enumerates no directory

#### Scenario: An upkeep item arrives once at a caller's session start

- **WHEN** a caller's first activation of a session occurs and a settled, egress-admitted proposal exists
- **THEN** that packet carries at most one upkeep item with its evidence, route, review route and triage verbs
- **AND** later activations in the same session carry none

#### Scenario: A delivered item is disposed of through existing verbs without a whole-vault scan

- **WHEN** the agent reviews, triages or follows the route of a delivered item
- **THEN** the item and its context are revalidated from its own subject and evidence pages only, a triage decision binds to its current fingerprint, and the next worker pass resolves an item whose route was applied

#### Scenario: An ignored item stops recurring until its evidence changes

- **WHEN** an item is delivered twice without a disposition, or is dismissed
- **THEN** no later session receives it again until its supporting evidence changes and the detector reproduces it
- **AND** dismissing one direction of a proposed link holds the pair

#### Scenario: A failing dreamer is visible

- **WHEN** the worker fails repeatedly
- **THEN** a session-start packet carries a status-only upkeep block naming when it began failing, and operator status reports it

#### Scenario: Ambiguity is reported, never proposed over

- **WHEN** a family meets an identity ambiguity
- **THEN** it proposes nothing for it and reports the ambiguity under the existing audit category that owns the defect

#### Scenario: Conversation recaps name a page that has not caught up

- **WHEN** live recaps of two or more episodes link an active governed page after its last update, and the page neither links nor cites them
- **THEN** one curation work item over the page and those recaps is proposed
- **AND** revisions of one episode count once, and the item resolves when the page is updated or links them

#### Scenario: A linked page carries no summary

- **WHEN** pages of two or more independent origins link an active governed page that has no `summary` field and no non-empty `## Summary` section outside code
- **THEN** `edit_memory` setting its `summary` is proposed

### Requirement: Alias and convention upkeep are corpus-derived and audience-exact

The alias/anchor family SHALL propose adding a spelling to a page's `aliases` only when other pages refer to that page by an unresolved link whose shared `vocabulary_fold.fold_term` equals one of its names, and SHALL name `edit_memory` as the route. Its proposal SHALL be identified by the fold key alone and served on the one page carrying the name that the caller may see. The convention/category family SHALL propose a tag spelling choice only for a fold-equal cluster of two or more authored spellings, without the server declaring a canonical spelling. It SHALL propose a category label change only against the semantic-language registry, meaning a label that folds to exactly one registered category or reviewed alias, or one whose definition names `replaced_by`. Both families SHALL derive their evidence from page contributions held in the dreamer's own sidecar, the published graph and the parse cache. They SHALL NOT use a model, turn text, the activation miss counter, a vault write or a walk, and SHALL stay within the tick's page and CPU budgets with at most 16 names, targets, tags or labels per page. A fold key carried by more than 32 pages a caller may see SHALL NOT be served to that caller, and pages withheld from that caller SHALL NOT count toward that bound. A fold key that reaches two or more pages' names is an identity ambiguity and SHALL NOT be proposed over. Under a governed policy every served field, count, fingerprint, liveness check, settle time, order and integrity count of these items SHALL be computed from released members only, so that a withheld page is indistinguishable from an absent one. No row cap SHALL evict their rows. Past the sidecar's size cap, which SHALL scale with the pages indexed, they SHALL record and deliver nothing until the file is back under it, and SHALL then reseed. They have two residuals, both the same for every caller: that size cap, and the timing of a request, whose cost may grow with withheld rows while its output does not. Candidate identity SHALL be independent of the producer, so that a later correction producer can support the same proposal.

#### Scenario: Other notes name a page by a variant spelling

- **WHEN** two notes link a name that folds to a page's title but does not resolve, and no other page carries that fold
- **THEN** one alias proposal names the page, the referring notes and their spelling, and routes to `edit_memory` on the page's `aliases`
- **AND** the proposal's reference does not depend on any page's path
- **AND** when the page gains the alias, or a second page gains that name, the proposal resolves or is withheld as an ambiguity

#### Scenario: A learned name confirmed by the corpus is offered for promotion

- **WHEN** a variant spelling other notes link is already one of the page's `learned_aliases`
- **THEN** the proposal's reason is `learned_alias_referenced` and it proposes the link-resolving `aliases` field

#### Scenario: Tag spellings drift across notes

- **WHEN** notes carry fold-equal tags in two spellings
- **THEN** one proposal per cluster is served on a note carrying the minority spelling, with the released count of each spelling, and the agent chooses the spelling
- **AND** the proposal's reference does not depend on any member page's path

#### Scenario: A category label is a variant of a registered one

- **WHEN** a unit's category label is unregistered but folds to exactly one registered category, or names a definition with `replaced_by`
- **THEN** a proposal for that page names the registered label and routes to `edit_memory`, and a registry change moves its fingerprint

#### Scenario: A withheld member equals an absent one

- **WHEN** the subject, a competing-name page or the majority-spelling pages are withheld from a caller
- **THEN** item, context, triage and the carrier return exactly what they return on a vault where those pages do not exist, including the served fingerprint and every count

#### Scenario: Withheld pages do not count toward the member bound

- **WHEN** more than 32 pages carry a fold key but no more than 32 of them are released to a caller
- **THEN** item, context, triage and the carrier return exactly what they return on a vault where the withheld pages do not exist

#### Scenario: A withheld member's activity is invisible

- **WHEN** only a withheld member page changes, or withheld pages make a name ambiguous for the owner
- **THEN** the caller's items, their order, their delivery time and the integrity counts are those of a vault where the withheld pages do not exist

#### Scenario: A reseed does not deliver half-counted clusters

- **WHEN** the sidecar is reseeding
- **THEN** no alias or convention item is deliverable until the reseed drains

### Requirement: Upkeep rides the activation packet as an optional bounded block

A caller-session-start activation packet MAY carry an `upkeep` block holding at most one proposal. The item SHALL be counted in the packet's `used_chars`, SHALL pass the same egress release decision as any unit so that a withheld page never appears in it nor in any count, SHALL be omitted whole rather than truncated, and SHALL be served only while every evidence signature equals the live one. The block SHALL NOT be `due_state` and SHALL NOT read or advance the due-state emission ledger. It SHALL be skipped when the request budget is spent, when structural suggestions are off, when the caller cannot be keyed, and on any error. A vault SHALL receive at most one item per 10 minutes across callers, a caller at most 3 per day, and one item at most two deliveries, the second at least a week after the first and to another caller.

#### Scenario: Upkeep never makes a turn late or leaks

- **WHEN** a session-start activation carries an upkeep item on a governed vault
- **THEN** the request enumerates no directory and stays within the activation ceilings
- **AND** an item whose subject is withheld from this audience is skipped with nothing about it in the packet

#### Scenario: A missing or locked sidecar attaches nothing

- **WHEN** the sidecar is absent, locked or unreadable at a session start
- **THEN** the packet carries no upkeep item and the request does not wait

### Requirement: Frozen verifiers are optional review labels only

Any frozen verifier SHALL obey the existing canonical `frozen-verifiers` admission and effects requirements, remain default-off, version-pinned, separately admitted and limited to review labels, and to evidence for sensed families under the `sensed-epistemic-model` capability, with abstention. It SHALL NOT author knowledge, select canonical identity, control retrieval/ranking, gate capture or define policy. Failure, abstention or resource pressure SHALL remove optional assistance without changing online semantics. CPU-first admission SHALL measure quality, false positives and resource interference; GPU use SHALL require separately verified co-tenant capacity. Programme acceptance SHALL include an admission decision with evidence, not mandatory model enablement.

#### Scenario: A verifier disagrees or cannot run

- **WHEN** optional verification abstains, fails or emits a disputed label
- **THEN** the active agent can inspect original evidence and the ordinary governed workflow remains available
- **AND** the label cannot directly change a canonical fact, authority decision or retrieval result

#### Scenario: Readings reach upkeep only as sensed families

- **WHEN** an admitted instrument's readings exist while upkeep proposals are produced and delivered
- **THEN** no structural family, carrier or review surface calls an instrument, and every structural family's items, order and caps are identical to a run with sensing disabled
- **AND** a reading reaches the agent only through a sensed family that the `sensed-epistemic-model` capability governs, and enters canon only through a write the agent authors that cites the reading
