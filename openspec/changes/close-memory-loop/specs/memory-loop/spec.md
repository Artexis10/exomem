## Purpose

Make ordinary conversation enrich durable knowledge and supply useful context later through one governed, observable loop across supported agent clients.

## ADDED Requirements

### Requirement: Ordinary use closes the capture and activation loop

The system SHALL support pre-turn activation, active-agent reasoning, episode decomposition before destination selection, canonical resolution and permitted writes, projection publication and subsequent activation. Delivery SHALL demonstrate ordinary-agent initiation without user reminders as well as deterministic tool execution. The active agent SHALL remain the semantic decision maker; server code SHALL validate and execute bounded typed operations without authoring semantic conclusions.

#### Scenario: Rich turn improves a fresh conversation

- **WHEN** an ordinary supported episode contains several durable changes and a later session needs them
- **THEN** the agent routes those changes to their canonical homes and the later activation supplies the relevant provenance-bearing facts without a save or recall reminder
- **AND** the record distinguishes observed agent behaviour from forced-call infrastructure tests

### Requirement: Working continuity preserves temporal meaning

Activation and episode recovery SHALL reconnect a resumed topic with relevant recent changes, supported current state, unresolved work and older dependencies within existing context budgets. They SHALL use the existing continuity and canonical evidence carriers without depending on the later live-activity feed. An unfinished episode SHALL NOT itself establish an unexpressed Planning commitment.

Where evidence supplies them, event occurrence, knowledge acquisition and claim validity SHALL remain distinguishable. Missing times or validity SHALL remain unknown. Capture time, file modification time, repetition and recent retrieval SHALL NOT independently establish event recency, continuing validity or task relevance. Corrections and supersession SHALL qualify current claims, while an older relevant dependency SHALL remain eligible beside recent developments.

The recent-context block SHALL judge recent work by the hot profile's own rules: a last edit inside a write burst, or older than the latest such burst, SHALL NOT make a page recent work; a page offered for its reads SHALL be ranked by its reads and not by its last edit, and the most-read such page SHALL keep a place in the block however many pages were edited more recently; and a retired or superseded page SHALL NOT be offered. An entry's one-line statement SHALL be the page's authored summary where it has one, and SHALL NOT be a lifecycle status such as `active` or `draft`.

#### Scenario: A topic resumes after an intervening development

- **WHEN** a supported session resumes an interrupted topic after a relevant development and an unrelated newer event
- **THEN** activation supplies the relevant development, older dependency and supported unfinished state without the user identifying their earlier conversations
- **AND** unrelated freshness cannot crowd out that context or manufacture a new user commitment
- **AND** acceptance observes the later agent response using those connections, separately from scripted tool calls

#### Scenario: A turn that resolves nothing still carries recent context

- **WHEN** a supported session sends a turn that names no anchor and resolves none, such as "continue" or "where were we"
- **THEN** activation still returns the bounded recent-context block, first in the packet, naming what was recently worked on with its provenance and the reason each item is recent
- **AND** no other block claims an anchor was resolved, and every item in the block crosses the same release plane a served unit does
- **AND** the block's contact times describe the edit, read or capture, not the events the pages record

#### Scenario: A batch, stale edits and retired pages do not fill the recent-context block

- **WHEN** a maintenance batch rewrote several pages after the user's last edit,
  the vault holds a retired and a superseded page edited more recently than
  anything else, and pages the user reads often were last edited long ago
- **THEN** the block offers neither the batch, nor any edit older than it, nor
  the retired or superseded page, and offers an edit made after the batch
- **AND** the most-read page appears in the block although many pages were
  edited more recently

#### Scenario: Old information is saved again after a correction

- **WHEN** an old event or superseded claim receives a recent capture or file edit without new event evidence
- **THEN** the system preserves its historical timing and applies the supported correction when presenting current state
- **AND** the fresh write timestamp does not make the old event new or the superseded claim current

### Requirement: Canonical ownership and provenance survive routing

Stable identity/facets SHALL belong to entities, mutable state/events to Records or appropriate domain stores, and original provenance to Sources/Evidence. Hubs SHALL be navigation/projections. Markdown semantic units and supported structured collections SHALL remain canonical; graph/search/profile stores SHALL be rebuildable with explicit currency. Routing SHALL preserve direct verification, reported claims, user hypotheses, attributed interpretations, inferences and uncertainty distinctly and SHALL NOT invent user Planning commitments. A durable user hypothesis or interpretation SHALL retain speaker or source attribution and uncertainty and SHALL NOT be silently omitted or represented as a direct fact.

#### Scenario: One conversation creates several destinations

- **WHEN** a supplier discussion includes a direct label observation, a reported formulation and an experiment exposure
- **THEN** their provenance and epistemic status remain distinct across entity knowledge and Records events
- **AND** an exposure is not upgraded into a causal conclusion or an unexpressed plan

#### Scenario: An attributed interpretation remains distinct from observation

- **WHEN** an episode preserves direct observations alongside a user-attributed family interpretation
- **THEN** the interpretation retains its source and uncertainty through canonical routing and later activation
- **AND** activation does not present it as a direct observation or silently drop it from the durable episode account

### Requirement: Dynamic identities and relations use normal public surfaces

The system SHALL resolve and enrich existing entities before creating duplicates and SHALL support meaningful first-mention promotion and independent recurring evidence without promoting incidental names. Registered and newly registered types SHALL work through MCP, CLI and REST creation, inspection, retrieval and typed traversal. Meaningful relationships SHALL reuse truthful registered definitions or receive governed extension proposals; generic relations SHALL NOT be the mandatory fallback for unsupported reusable distinctions.

#### Scenario: A registered equipment kind crosses adapters

- **WHEN** a permitted equipment entity is created through MCP under a dynamically registered type
- **THEN** CLI and REST observe the same canonical identity, stable facets and typed relationships without requiring a generic file-writing route

#### Scenario: A recurring alias points to an existing entity

- **WHEN** supported durable facets recur under an alias with a resolved existing identity
- **THEN** the agent proposes or applies permitted hydration of that identity instead of creating a duplicate
- **AND** ambiguous or incidental mentions do not cause automatic identity assignment

#### Scenario: A useful reusable relationship is missing

- **WHEN** no current registered relation truthfully expresses a supported reusable distinction
- **THEN** the active agent may propose a governed namespaced extension with precise semantics, core parent/family and directionality/symmetry, validate it and publish it through the existing vocabulary workflow before ordinary use
- **AND** pending or failed publication is not presented as an active definition
- **AND** generic or absent edges remain legitimate when greater precision lacks evidence

### Requirement: Destination acceptance observes ordinary-agent decisions

The existing capture-to-activation benchmark SHALL include a separately named synthesis and a paired minor-refinement negative. Original input and the pre-capture vault snapshot SHALL be frozen before execution, with evaluator expectations withheld from the actor. The run SHALL observe relevant antecedent retrieval, destination decisions before effects, actual public-writer receipts, graph/index publication and subsequent useful activation. Scripted correct writes and repaired post-capture state SHALL NOT establish no-nudge success. Exact private replay SHALL require recoverable original input and the appropriate snapshot; reconstructed fixtures SHALL be labelled separately.

#### Scenario: An agent chooses the focused destination without a correction

- **WHEN** a synthetic ordinary product discussion synthesizes five narrower antecedents into an independently useful named thesis
- **THEN** the first capture pass creates its focused home and truthful typed links without a save, routing or page-creation instruction
- **AND** a later context activation retrieves the thesis with provenance
- **AND** the matched refinement case stays on its existing page without fragmentation

#### Scenario: Only the corrected note survives

- **WHEN** the original private episode or its pre-capture snapshot is unavailable
- **THEN** exact replay remains unmeasured
- **AND** a reconstruction from the corrected note cannot be reported as a passing original no-nudge replay

### Requirement: Independent referents remain distinct from roles

The system SHALL support separately evidenced operator organizations, physical sites and independently useful brands without conflating their identities. One entity MAY carry several governed roles/facets without duplicate identities per role. Canonical types and optional parent families SHALL use the existing dynamic registry; filesystem paths SHALL remain projections. The possibility of independent change SHALL inform active-agent consideration but SHALL NOT alone justify entity creation. Existing identity and alias resolution, hydration, authority and provenance rules SHALL apply before creation or restructuring.

#### Scenario: A site changes operator

- **WHEN** evidence establishes a physical site, its current operator and a later operator transition
- **THEN** the site retains its identity and the operators remain distinct identities
- **AND** historical relationships retain their evidence and temporal qualification while supported current state reflects the transition

#### Scenario: One organization fills several roles

- **WHEN** one organization is a producer, operator and supplier
- **THEN** the agent records supported roles/facets or relationships on that identity without creating three organizations
- **AND** an incidental trading name does not automatically create a separate brand

#### Scenario: A brand has an independently useful identity

- **WHEN** evidence establishes a durable brand whose ownership or usage can change independently of an existing organization
- **THEN** the agent resolves or proposes its separate identity through the normal governed type/entity path and preserves supported relationships
- **AND** the organization is not duplicated or retyped as the brand

### Requirement: Contextual name resolution preserves genuine ambiguity

Exact names and aliases that have multiple real-world referents SHALL return bounded candidates with explicit omission information. The active agent SHALL decide semantic reuse, a justified distinct identity or abstention using authorized context and evidence. Deterministic infrastructure SHALL NOT apply domain-specific context rules to select a referent, silently transfer an alias or merge identities. A justified distinct identity SHALL remain expressible through a governed public writer despite overlapping surface names; existing aliases SHALL NOT be reassigned implicitly.

#### Scenario: One name denotes both a company and its site

- **WHEN** an episode uses a surface name shared by an organization and a physical site
- **THEN** the resolver preserves both candidates and the agent can select the evidence-supported identity for each claim
- **AND** insufficient context leaves the claim unresolved without a write to an arbitrary target
- **AND** later activation preserves the same ambiguity rather than treating the alias as globally owned by one node

### Requirement: Supplier topology preserves evidence for each relationship

Every authored entity and durable edge in the integrated loop SHALL retain attributable source, evidence or originating governed-write provenance and its supported epistemic status. Registered relations SHALL be reused where truthful and missing reusable meanings SHALL follow governed extension rules. Seller, operator, producer, production site and purchased lot SHALL remain distinguishable when evidence establishes them. Mutable lot/event provenance SHALL remain in Records or canonical domain stores. Graph traversal SHALL NOT turn inferred transitive paths into directly sourced edges or general sourcing relationships into unsupported lot attribution.

#### Scenario: One seller supplies products from several producers

- **WHEN** a purchase episode identifies one lot from the seller's own site and another from a partner producer
- **THEN** capture resolves existing identities and preserves each lot's supported origin and source attribution separately
- **AND** fresh-session activation returns the correct provenance for the requested lot within existing budgets
- **AND** an otherwise identical lot with unknown origin stays unresolved even when the seller's partner list is known

#### Scenario: Dynamic site capture completes the ordinary loop

- **WHEN** an ordinary supported episode establishes a recurring physical site, operator and evidence-bound relationships
- **THEN** the active agent considers their canonical homes before writing, uses permitted normal type/entity/relation operations, publishes the resulting graph and performs the bounded coverage check without a user nudge
- **AND** MCP, CLI and REST agree on identity and later activation consumes the resulting topology without a generic file writer or a forced organization classification
- **AND** deterministic forced-call tests and observed ordinary-client initiation are reported separately

### Requirement: Client guarantees reflect observed lifecycle support

MCP, CLI and REST SHALL expose the same core semantics and versioned capabilities. Acceptance SHALL cover Claude Code, Codex, ChatGPT, Claude app and a generic MCP client, recording adapter versions, tool parity, lifecycle enforcement, recovery and observed initiation separately. A host exposing lifecycle hooks or equivalent middleware SHALL enforce bounded checkpoints and pending continuation. A tool-only host SHALL receive bootstrap/skill guidance and SHALL report initiation as best effort, without promising interception of unseen tool-free turns.

#### Scenario: A client has tools but no turn hook

- **WHEN** a client supports MCP operations but no lifecycle interception
- **THEN** it can perform the full workflow with the shared tools and its capability report says best-effort initiation
- **AND** tool availability alone is not reported as automatic per-turn execution

#### Scenario: A client exposes custom-scheme link targets

- **WHEN** an ordinary answer cites a memory result and the client would display its opaque reference
- **THEN** the answer follows the existing title-first presentation contract with a plain readable title and an optional human-readable disambiguator
- **AND** stable identity remains available internally without becoming the default visible label

### Requirement: Canonical vocabulary precedes destination projection

Normal writes SHALL share family-aware resolution over existing canonical vocabularies before projecting filesystem destinations. Unique normalized exact equivalents and reviewed aliases SHALL reuse canonical identity deterministically. Ambiguous existing equivalents SHALL be surfaced without arbitrary selection. Non-exact proposals SHALL receive bounded authorized candidate definitions and representative usage for the active agent to decide reuse, enrichment, a distinct new identity or deferral. The server SHALL NOT reason about semantic equivalence, silently merge neighbours, create aliases or close an otherwise open vocabulary.

Resolution SHALL preserve each family's validation and authority rules. Mutation receipts SHALL expose requested token, resolved canonical identity and final destination. Existing serialized write boundaries SHALL revalidate the chosen identity and destination before commitment across clients. Existing provenance and duplicate trees SHALL NOT be silently moved or merged.

The first Notes-domain slice SHALL use a strict write-side registry snapshot and bind canonical metadata, identity, destination projection and agent disposition into immutable preparation. Read/bootstrap fallback SHALL NOT authorize a write under an unreadable or ambiguous registry. Validation, committed, replayed, compact and full responses SHALL preserve the bounded `vocabulary_resolution` fields `family`, `requested`, `canonical`, `destination`, `match_kind` and `snapshot`. The family SHALL be `domain`, shared across its projection adapters. Evidence incident/case/project scopes SHALL NOT inherit subject-domain alias or slug semantics.

#### Scenario: Existing canonical spelling receives a case variant

- **WHEN** Health already identifies an experiment domain and a normal write requests health, or Food exists and the request is food
- **THEN** the write reuses the unique canonical identity and destination without creating a case-only sibling
- **AND** the receipt exposes both the request and the resolved result

#### Scenario: Reviewed alias and semantic neighbours remain distinct cases

- **WHEN** a reviewed alias names an existing identity
- **THEN** the normal write resolves it deterministically across participating writers
- **WHEN** a new development proposal has software-engineering as a nearby existing meaning
- **THEN** the agent receives bounded candidates and decides whether to reuse or create a distinct identity
- **AND** a legitimate health/wealth distinction remains possible without automatic merging

#### Scenario: Registry ambiguity cannot be resolved by entry order

- **WHEN** the write registry is unreadable, malformed, or contains duplicate normalized canonical/alias owners or equivalent path-label collisions
- **THEN** the write returns typed invalid-registry or ambiguity information without changing canon
- **AND** it does not use permissive read defaults or choose the last registry entry

#### Scenario: Alias resolution governs metadata and public receipts

- **WHEN** an experiment uses a reviewed domain alias and a unique existing projection spelling
- **THEN** persisted metadata and activity use the canonical key, the existing destination is reused, and public compact/full/replayed receipts retain the requested token and canonical resolution
- **AND** changing a folder alone while leaving alias metadata does not satisfy acceptance

#### Scenario: A nearby meaning is presented before a destination is committed

- **WHEN** a non-exact domain proposal has nearby existing meanings
- **THEN** a typed non-mutating preparation exposes bounded authorized definitions and representative usage before minting a committable destination
- **AND** the active agent explicitly chooses reuse, create or defer against the returned evidence fingerprint without requiring a user confirmation
- **AND** no hidden model, semantic merge or post-write-only advisory substitutes for that decision

#### Scenario: A second client changes the vocabulary after preparation

- **WHEN** a second writer introduces a canonical equivalent before the first prepared write commits
- **THEN** commit revalidation reuses a still-valid canonical result or refuses for fresh resolution
- **AND** it does not create an equivalent sibling from the stale preparation

#### Scenario: A prepared draft crosses a registry or directory change

- **WHEN** either the registry or projection-directory membership changes after validation
- **THEN** both structural and relation-reviewed creation paths revalidate inside their existing commit boundary and preserve the identical binding or refuse for fresh preparation
- **AND** an immutable draft token is never silently retargeted

#### Scenario: Replacement or a composed plan needs a vocabulary decision

- **WHEN** replacement or a composed create-note or supersede step encounters a nearby domain meaning
- **THEN** it exposes the bounded preparation through its existing public result or error contract and accepts the same finite agent decision
- **AND** unresolved or deferred preparation leaves the predecessor unchanged and stores no committable destination or curation plan
- **AND** a resolved decision preserves the canonical binding and guards through the existing leaf executor

#### Scenario: Recovery resumes from a graph receipt or prepared relation artifact

- **WHEN** a committed Notes write is recovered from its graph receipt before the ordinary idempotency completion record exists
- **THEN** replay preserves the original bounded vocabulary resolution without repeating the effect
- **AND** resuming a prepared relation artifact retains the registry and directory guards through atomic commitment

### Requirement: Activation earns acceptance through useful bounded context

Activation SHALL resolve anchors, apply task-conditioned roles and bounded typed expansion, qualify current state and carry provenance under the existing context-activation budgets. Ambiguity, partial anchor coverage and stale/unavailable publication SHALL remain explicit. Acceptance SHALL exercise negative twins, rare/unfamiliar and multilingual referents, cross-kind links, supersession, Records state, distractor padding and an embeddings-on smoke path. Accepted context SHALL improve a later ordinary response without poison facts or unsupported resolution.

#### Scenario: A popular neighbour conflicts with a rare explicit anchor

- **WHEN** a task names a rare entity with useful current state and neighbouring popular material is irrelevant
- **THEN** the packet preserves the explicit resolved anchor and its relevant provenance within budget
- **AND** popularity does not substitute an unrelated entity or turn uncertain state into a fact

#### Scenario: A resolved project anchor serves its own member pages

- **WHEN** a turn names a project and the project's member pages declare that
  project as their own scope
- **THEN** the resolved project anchor's role lanes serve material from
  those member pages that the release plane admits for the current
  audience, bounded and most-recently-updated first
- **AND** a member page the current audience may not see contributes no unit
  and no pointer, through the same release-plane guard as any other
  neighbourhood page
- **AND** a project anchor with no declared members still resolves and
  reports no material, rather than fabricating any

### Requirement: Activation semantic work stays within the anchor catalogue

Activation SHALL use the derived anchor signature vectors for optional semantic
corroboration instead of unconditionally invoking ordinary full-vault hybrid
recall. It SHALL NOT load full note-chunk or multimodal vector matrices on the
activation path. Ordinary recall behaviour SHALL remain unchanged. One query
encode MAY use the configured resident encoder only under nonblocking model
admission; activation SHALL NOT load a cold model or wait behind another model
operation. Existing categorical evidence and ambiguity rules SHALL apply to
all candidates, including competitors of an exact match. Vector evidence alone
SHALL NOT resolve an anchor.

Lean activation SHALL retain bounded own-page lexical retrieval evidence from
the maintained full-page FTS catalogue restricted to anchor paths. Anchor
RESOLUTION SHALL see no lexical evidence from outside that restriction. The
lexical query SHALL preserve governed overfetch and per-audience release before
its paths become evidence, SHALL NOT rebuild or apply a foreground delta, and
SHALL NOT fall back to an in-process corpus scan. Its readiness result SHALL be
reported as `generation.lexical_evidence`; incomplete publication SHALL remain
non-cacheable. Title/alias overlap alone SHALL NOT be relabelled as retrieval.
Lexical corroboration SHALL match at least two distinct content stems from the
turn after the shared stopword filter, with that predicate applied before the
ranked result limit. Repeated or inflected forms of one stem SHALL NOT provide
the second match. This restriction SHALL NOT change ordinary recall.
Categorical lexical-overlap evidence SHALL additionally require genuine name
contact: two or more of the shared broad terms among the anchor's own
authored title/alias terms. A single shared authored term SHALL NOT by
itself grant lexical-overlap evidence, however independently rare that one
term is by the same yardstick rare-term evidence uses — rarity among the
catalogue's anchor names is not rarity of the word, and treating the two as
the same let an ordinary word that happens to name few anchors resolve an
unnamed anchor. A single shared authored term MAY still grant the separate,
weaker rare-term evidence when it is independently rare; an unavailable
rarity table SHALL NOT be read as proof of rarity, and rare-term evidence
alone or with only a qualifier SHALL NOT resolve an anchor. A shared term
written entirely in ASCII letters and shorter than three characters SHALL NOT
grant rare-term evidence, because rarity among anchor names cannot tell a
genuinely short name from an everyday two-letter word a title happens to
contain; a term carrying any other character is exempt. The one exception SHALL
be a two-letter acronym both sides spell as one: the turn writes it as exactly
two capital letters and the anchor's own authored title writes it in capitals
too. A single capital, a dotted abbreviation, capitals in a turn with no
lower-case letter, and capitals the anchor's title does not share SHALL NOT
qualify; a one-letter name SHALL remain reachable through its own spelling.
Independently resolved items sharing a nonempty canonical page or collection
SHALL be treated as complementary rather than competing senses. Empty paths
SHALL NOT establish that relationship. Disconnected same-kind groups SHALL
remain ambiguous even when one group has several complementary items.
Ambiguity choices SHALL name each canonical home once, and a choice of that
home SHALL retain its complementary items. Distinct canonical refs SHALL
remain individually selectable; choosing one SHALL NOT mark its graph
neighbours as explicit agent choices.

The activation request SHALL reuse one freshness snapshot and full recall
checkpoint across lexical and role queries without weakening transaction-bound
catalogue proof or current-parent validation. Unit role queries SHALL restrict
parent paths to the resolved anchor neighbourhood before applying their row
limit. Missing or stale publication SHALL remain explicit and SHALL NOT trigger
foreground repair. Managed-service warm latency and offline cold filesystem
proof SHALL be reported separately.

Packet generation metadata SHALL identify semantic evidence as ready, disabled,
absent, warming, busy, unavailable or unnecessary for an explicit agent choice.
Transiently incomplete evidence SHALL NOT create a reusable packet cache entry.
Every packet SHALL still cross the current governance release plane, including
cached packets and packets without semantic evidence. End-to-end latency
acceptance SHALL include semantic work and useful context, not just an empty
fast abstention or compiler-only timing.

#### Scenario: An exact name has a weak competing sense

- **WHEN** the turn exactly names one resource and independently reaches a second
  same-kind resource through a rare term plus a signature vector match
- **THEN** both participate in resolution and the existing ambiguity rule applies
- **AND** no ordinary full-vault recall or multimodal matrix is acquired

#### Scenario: Another request occupies model execution

- **WHEN** the resident encoder cannot acquire model execution immediately
- **THEN** activation reports busy semantic evidence and uses only available
  structural evidence without queuing an encode or caching the incomplete result
- **AND** a later uncontended request can add genuine signature evidence

#### Scenario: Signature similarity has no worded contact

- **WHEN** a signature is semantically similar but has no independently deciding
  worded evidence or valid continuity qualification
- **THEN** it remains partial and supplies no context-role material

#### Scenario: A lean installation reaches a resource through its contents

- **WHEN** embeddings are disabled, a turn carries a rare resource term and
  corroborating content ranked from that resource's own current page
- **THEN** the resource can retain `rare_term` plus `retrieval` resolution without
  loading any model, vector matrix or full-vault search
- **AND** a stale catalogue contributes no evidence and is not repaired inline

#### Scenario: One shared word has no independent corroboration

- **WHEN** a turn shares one rare authored term with an anchor and its own-page
  text match supplies only that same stem plus function words
- **THEN** text retrieval does not resolve the anchor through a second vote
- **AND** repeated or plural forms of the shared word do not change that result

#### Scenario: A single common name word is not name contact

- **WHEN** a turn shares exactly one authored title/alias term with an
  unnamed anchor's long title, completed to the broad-term band only by a
  section or tag word the turn also happens to share, and that one authored
  term is not independently rare
- **THEN** the anchor earns no lexical-overlap or rare-term evidence from
  that overlap and does not resolve on it alone
- **AND** two or more shared authored terms still grant lexical-overlap
  evidence as before

#### Scenario: A single rare name word is contact, never overlap, and needs its own corroboration

- **WHEN** a turn shares exactly one authored title/alias term with an
  anchor that IS independently rare by the anchor-name-count yardstick,
  completed to the broad-term band by a section or tag word, with only a
  turn-cue qualifier and no independently reached contact
- **THEN** the anchor earns rare-term evidence, never lexical-overlap, and
  stays partial
- **AND** the same anchor with its own page in retrieval instead resolves,
  via rare-term evidence, never lexical-overlap

#### Scenario: Capitals alone never make a short word a lead

- **WHEN** a turn writes a one- or two-letter word in capitals — a grade ("I
  got a C"), emphasis ("should I GO with the cheaper one?") or a dotted
  abbreviation ("U.S.") — and an anchor's title shares that word without
  writing it in the same capitals
- **THEN** the anchor earns no rare-term evidence from it
- **AND** a turn writing "AI" still earns rare-term evidence for an anchor
  titled "AI Subscriptions"

#### Scenario: A collection contains complementary Planning items

- **WHEN** an outcome and an action resolve to the same canonical collection
- **THEN** the compiler may serve their context without reporting ambiguity
- **AND** genuinely disjoint collections still participate in ambiguity checks

#### Scenario: A unit crosses the release plane by its real page, not its opaque ref

- **WHEN** a packet unit carries the compiler's opaque `exomem://vault/<path>#unit-<hash>` reference under an active governance policy
- **THEN** the release plane decides the real vault page that reference names, not the literal reference text
- **AND** a policy scoped to an unrelated page leaves the unit served, and a policy scoped to only the unit's own page withholds that unit without withholding an unrelated one
- **AND** a reference that does not unwrap to a path inside the vault stays undecidable and withheld exactly as before

### Requirement: A turn that resolves no anchor MAY be carried by one dominant recall hit

Retrieval evidence alone SHALL NOT resolve an anchor. Where a turn resolves no
anchor at all, activation MAY instead CARRY a packet from a single compiled
knowledge-base page, and this SHALL be the only case in which retrieval alone
produces served material.

The carry SHALL run only when anchor resolution returned no resolved anchor and
the request named no explicit anchor choice; an ambiguous turn, a turn that
resolved any anchor and a referential turn SHALL be untouched. A hit SHALL be a candidate only where at least two
of the turn's stems that it matches are DISTINCTIVE in the indexed corpus,
measured as a document frequency at or below `max(3, ceil(0.5% of the indexed
pages in scope))` over the same catalogue the ranking uses, navigation pages not
counted toward a stem's frequency, and raw-material pages counted neither toward
a stem's frequency nor among the indexed pages. A hit SHALL additionally satisfy a proximity
condition: two of its matched distinctive stems occur within a declared token
window of each other, within one sentence of the turn. Distance SHALL be
measured over the turn's own tokens, function words included; sentence-ending
punctuation and line breaks SHALL end a window and a comma SHALL NOT. A
number of distinctive stems occurring anywhere in the turn SHALL NOT by
itself admit a page. A turn carrying fewer than two distinctive
stems SHALL carry nothing and SHALL NOT run the ranking query. Where the indexed corpus holds fewer than a declared minimum
number of pages, the carry SHALL NOT run at all: rarity is only as sharp as
the corpus it is measured against, and below that size there is no corpus to
measure against. An absolute score threshold SHALL NOT be used to decide contact:
the ranking score is comparable only between hits drawn from one corpus. The
query SHALL run once against the maintained full-page catalogue over the
knowledge-base scope, without the anchor path restriction, applying the same
no-foreground-delta and no-corpus-scan rules, and reporting rather than
repairing an incomplete catalogue. Pages in the knowledge base's raw-material
folders — captured sources and preserved evidence, the same folders the anchor
catalogue already refuses to build an anchor from — SHALL NOT be candidates, and
neither SHALL navigation pages, identified by the same navigation-page rule the
recall corpus uses, since they repeat the titles of the pages they list. The
query SHALL run within the request deadline
under its own timing span and SHALL be skipped when that deadline can no longer
afford a stage.

Retired pages SHALL NOT be candidates, and lifecycle SHALL be decided before
candidates are counted. Retirement SHALL be the system's own inactive page
vocabulary less the statuses meaning pre-active rather than retired, together
with a declared supersession; a draft or planned page SHALL remain a
candidate. The number of ranked rows read before filtering SHALL exceed the
number of pages the rarity gate can admit at the current corpus size. Candidates SHALL be excluded before they are counted, not after the ranked
result is limited, and raw-material and navigation pages SHALL be excluded
inside the ranking query, before its row limit applies, so that the limit
counts only rows that can be candidates. A page SHALL be carried only when it is the ONLY surviving
candidate; where two or more survive, the turn SHALL abstain `unresolved`,
SHALL NOT select between them by ranking score, and SHALL report them under
`anchors[]` at a distinct status meaning "named, not carried" — never the
carried status and never as resolution ambiguity. Those entries SHALL cross
the release plane as anchors do. A candidate
the ranking scored at or below a declared sanity bound SHALL NOT be carried.

A carried packet SHALL report that page as its one anchor entry, of kind `page`,
at status `retrieval_carried`, with `retrieval` as its only evidence, and SHALL
mark itself `generation.carried_by = "retrieval"`. Its material SHALL come from
that page through the existing bounded unit lanes. The continuity token minted
from a carried packet SHALL name the carried page's path, so that a following
referential turn can resume it; continuity SHALL still qualify only an anchor a
later turn reached. The carried page SHALL cross the same release plane
guard as any other packet reference, and a carried page the current audience may
not see SHALL abstain `withheld` rather than substitute another candidate.

#### Scenario: A decision living in a research note is reachable

- **WHEN** a turn names no anchor of the catalogue but repeats one compiled
  research note's own words, and recall places that note clearly ahead of every
  other candidate
- **THEN** the packet serves that note's units under one anchor entry of kind
  `page` at status `retrieval_carried`, marked as carried by retrieval
- **AND** no continuity token is minted from it, and no anchor is resolved

#### Scenario: A corpus too small to measure rarity carries nothing

- **WHEN** a turn that resolves no anchor reaches a page in a knowledge base
  holding fewer than the declared minimum number of indexed pages
- **THEN** the carry does not run and the turn abstains `unresolved`
- **AND** the same turn against the same page IS judged on rarity once the
  corpus is large enough to measure it

#### Scenario: Two distinctive words far apart are a coincidence

- **WHEN** a turn that resolves no anchor shares exactly two distinctive
  stems with a page and says them further apart than the declared window
- **THEN** that page is not a candidate and the turn abstains `unresolved`
- **AND** the same two stems said as a phrase DO make it a candidate, as do
  three of that page's distinctive stems sitting anywhere in the turn

#### Scenario: A stub sharing only ordinary words is not named

- **WHEN** a turn that resolves no anchor shares two or more stems with a page,
  and none of those stems is distinctive in the indexed corpus
- **THEN** that page is not a candidate and the turn abstains `unresolved`
- **AND** the same page IS a candidate for a turn that shares two distinctive
  stems with it, at any corpus size

#### Scenario: A turn that names two pages carries neither

- **WHEN** a turn that resolves no anchor names two compiled pages, at any
  ranking scores whatever
- **THEN** the packet abstains `unresolved` with no units, exactly as it did
  before the carry existed
- **AND** the higher-scoring page is not served in preference to the other
- **AND** both pages are listed as named-but-not-carried so the caller can ask
  for one, with any the current audience may not see removed from that list

#### Scenario: A superseded page does not block the page that replaced it

- **WHEN** a turn names a page whose predecessor it superseded, and both
  answer to the same distinctive words
- **THEN** only the current page is a candidate and it is carried
- **AND** an archived page is likewise never carried

#### Scenario: A navigation page is never a named page

- **WHEN** a turn names one compiled page by its title's distinctive words,
  and the vault's index and log pages list that title among others
- **THEN** the named page is carried, and no navigation page is counted as a
  second named page or listed under `anchors[]`

#### Scenario: Captured sessions do not make a page's title ordinary

- **WHEN** a turn names one compiled page by its distinctive words, and three
  or more captured sessions in a raw-material folder repeat those words
- **THEN** the words remain distinctive and the named page is carried
- **AND** captures and navigation pages that the ranking scores above the page
  do not consume the rows read before filtering

#### Scenario: A named anchor and raw material are both refused as carriers

- **WHEN** a turn both names an anchor and matches a compiled page, or its
  strongest match is a page in a raw-material folder
- **THEN** the named anchor's own packet is served and is not marked as carried,
  and the raw-material page is never a candidate for carrying

#### Scenario: A carried page the audience may not see abstains

- **WHEN** the release plane withholds the dominant page from the current
  audience and the turn also reached a weaker candidate
- **THEN** the packet abstains `withheld` with no anchors and no units
- **AND** the weaker candidate is not carried in its place

### Requirement: An agent's own `anchor` choice MAY name an ordinary compiled page

The `anchor` override SHALL NOT be limited to a row of the activation index. Where
the chosen ref names no such row, activation SHALL instead test it against the
SAME eligibility the retrieval carry already applies to a page it carries on
recall alone: not a raw-material page, not a navigation page, and current —
existing, Markdown, and not retired. An eligible page SHALL be served exactly
as a retrieval-carried page is, through the existing bounded unit lanes, but
marked as the AGENT's own choice rather than recall's: its one anchor entry
SHALL be of kind `page`, at status `resolved`, with `agent_choice` as its only
evidence, and the packet SHALL mark itself `generation.carried_by =
"agent_choice"`. `anchor` naming a row of the activation index SHALL be
unaffected by this fallback and SHALL continue to resolve exactly as before.

A ref that names neither an activation-index row nor an eligible page, or one
the current audience may not see, SHALL be refused with the SAME error an
unknown or withheld anchor already shares; the refusal SHALL NOT distinguish
between an unknown ref, a withheld one, raw material, a navigation page, and a
retired page. `recent_context` SHALL remain the first block of a packet served
this way, exactly as it is for any other packet.

#### Scenario: An agent picks a page a packet already listed

- **WHEN** the agent calls again with `anchor` set to the ref of an ordinary
  compiled page a previous packet listed — under `recent_context`, as a
  `retrieval_named` page, or as an unresolved turn's own candidate — and that
  page is not a row of the activation index
- **THEN** the packet serves that page's units under one anchor entry of kind
  `page`, at status `resolved`, with `agent_choice` as its only evidence
- **AND** the packet marks itself `generation.carried_by = "agent_choice"`
- **AND** `recent_context` remains the packet's first block

#### Scenario: An override naming an activation-index row is unaffected

- **WHEN** `anchor` names a row of the activation index
- **THEN** it resolves exactly as it did before this fallback existed, at
  status `resolved` with `agent_choice` as its evidence, and the packet does
  not mark itself as carried

#### Scenario: A withheld page and an unknown ref refuse identically

- **WHEN** `anchor` names an eligible page the current audience may not see,
  or a ref this index does not recognize at all
- **THEN** both are refused with the identical error, naming neither the ref
  nor which rule excluded it

#### Scenario: Raw material, a navigation page and a retired page are refused like an unknown ref

- **WHEN** `anchor` names a page in a raw-material folder, a navigation page,
  or a page the vault has retired
- **THEN** each is refused with the identical error an unknown ref receives,
  and no packet is built

### Requirement: A turn that names nothing MAY resolve to the hottest recent anchor

A recency prior SHALL NOT resolve an anchor, except for a REFERENTIAL turn — one
whose own words are the reference and name nothing. This is the only case in
which a file modification time, a read count or a previous packet's own answer
may establish task relevance, and it SHALL be bounded by every condition below.

A turn SHALL be referential only when it speaks one of the declared referential
cues, matched on whole tokens rather than as a substring of a longer word, AND
says nothing else: once the matched cue words, function words and one closed,
declared set of filler words that refer to the work without naming it are
removed, no word SHALL remain. A turn that speaks a cue word in its ordinary
sense or names anything besides it ("update my resume", "check the status of my
flight", "what's next for <a page>") SHALL NOT be referential, and a turn SHALL
NOT be referential merely because it is short. Recency evidence SHALL
be a distinct evidence kind of its own class: it SHALL NOT be a worded contact
kind, SHALL NOT be a retrieved
contact kind, SHALL NOT count toward the two-kinds rule for any other kind, and
SHALL NOT be a tie-break between candidates the turn named. It SHALL be reported
in a resolved anchor's evidence so a reader can see why that anchor was served.

The hot profile SHALL be a bounded, deterministic projection over the anchor rows
the request already holds — a previous packet's continuity references, the
freshness registry's own last-edit times, and the maintained usage activation
snapshot — ranked in a single declared order, ties broken by a stable identity.
A previous packet's continuity references SHALL rank first and SHALL form one
tier taken whole: that packet already resolved them together — but only while
no anchor outside them has a last edit, not in a write burst, later than the
time that packet was served. While that tier leads, the profile SHALL NOT fall
through to the edit or read tiers: where the references name a compiled page
that is not an anchor — one the agent picked — that single page SHALL be
resumed from its own units — or one recall carried — reported `resolved` on `continuity` and `recency`
and marked `generation.carried_by = "continuity"`, crossing the release guard
like any unit, so a page the audience may not see abstains `withheld`; where
they name nothing that can be served, the turn SHALL abstain with its
recent-context block. A last-edit time
that fell in a write burst — a chain of a declared number of pages or more,
navigation pages not counted, each edited within a declared interval of the
next, so that one stall inside a batch does not split it —
SHALL carry no edit signal, because a batch rewrites pages nobody chose, and
neither SHALL a last-edit time older than the latest such burst, because the
batch may have rewritten the page the user was working on; such an anchor
SHALL be ordered by its reads alone, and with no reads either the profile
SHALL be empty rather than a menu or the freshest survivor. The profile SHALL
be computed only for a referential turn. It SHALL NOT enumerate directories or
raise any declared request-path ceiling, and SHALL read no page except, from the
request's own page cache, the bounded few at its top, to exclude one that another
page supersedes. A retired anchor SHALL NOT be in it. Recency evidence SHALL be
earned by the anchors at the TOP of that ranking only, and the number of anchors
that may tie at the top SHALL be bounded.

Recency SHALL resolve an anchor only where the turn is referential AND no
candidate anywhere in the same resolution carries a worded contact kind. Where
any candidate does, the named anchor SHALL be served and recency SHALL decide
nothing. Where two or more anchors of one kind that nothing structural relates
tie at the top of the profile, the turn SHALL report them as ambiguity for the
agent to choose between and SHALL NOT select one by recency; tied anchors of
different kinds, or of one kind that are structurally related, are
complementary and SHALL resolve together, as any two such anchors do. A
recency referent SHALL NOT be lost to a candidate or anchor bound: where recency
may resolve, the hot candidates and the resolved anchors SHALL be kept ahead of
partial candidates when those bounds are applied, and the prior SHALL NOT
change the order of candidates the turn reached by its own words. Where the profile is empty the turn SHALL
abstain exactly as before, still carrying its recent-context block.

A referential turn names nothing, so the retrieval carry SHALL NOT run for it;
a turn that names a page is not referential and is decided by the carry
exactly as a turn that resolved no anchor. Where every anchor a packet resolved
stood on recency alone, the rendered block SHALL say that its referent came
from recent work and not from the turn's own words.

#### Scenario: A referential turn resolves to the hottest recent anchor

- **WHEN** a session sends a turn that names nothing, such as "continue" or
  "where were we", and one anchor is hottest in the bounded recency profile
- **THEN** that anchor resolves, its evidence names the recency prior, and its
  role lanes serve its material like any other resolved anchor
- **AND** the same turn against an empty profile still abstains and still
  carries the recent-context block

#### Scenario: A referential turn with two equally hot anchors is ambiguous, never guessed

- **WHEN** two anchors of one kind tie at the top of the recency profile on a
  referential turn and nothing structural relates them
- **THEN** the turn reports both as ambiguity for the agent to choose between
- **AND** the server does not pick the one it happened to rank first

#### Scenario: A named anchor wins over recency

- **WHEN** a turn both names an anchor by its own words and the vault holds a
  hotter anchor the turn never mentioned
- **THEN** the named anchor resolves and carries the packet
- **AND** the hot anchor neither resolves nor completes any other candidate's
  evidence, on this or on any turn that is not referential

#### Scenario: A short turn with no referential cue is not answered by recency

- **WHEN** a turn speaks no referential cue, however few words it has, names no
  anchor, and one anchor is hottest in the recency profile
- **THEN** that anchor does not resolve and its material is not served
- **AND** the turn abstains `unresolved` still carrying its recent-context block,
  unless it names a compiled page the retrieval carry serves

#### Scenario: A cue word in its ordinary sense is not answered by recency

- **WHEN** a turn speaks a referential cue word but also says what it is about,
  such as "update my resume" or "what's next for <a compiled page>", and one
  anchor is hottest in the profile
- **THEN** the turn is not referential and the hot anchor is not served
- **AND** a turn that names a compiled page is decided by the retrieval carry,
  and abstains `unresolved` when the carry cannot run, never falling back to
  the hot anchor

#### Scenario: Continuing after a carried answer resumes the carried page

- **WHEN** a turn was answered by a retrieval-carried page, and the next
  referential turn passes the token of that packet
- **THEN** the token names the carried page, and that page resolves on
  `continuity` and `recency` with its units served, not the anchor the
  conversation held before the carried answer

#### Scenario: Continuing after the agent picked a page resumes that page

- **WHEN** the agent picked a compiled page that is not an anchor, and the next
  referential turn passes the token of that packet
- **THEN** that page resolves on `continuity` and `recency` and its units are
  served, and the vault's freshest anchor is not
- **AND** a page the audience may not see abstains `withheld`, and a page that
  no longer exists abstains `unresolved`, neither falling through to another
  anchor

#### Scenario: A maintenance batch does not pick the referent

- **WHEN** the user last edited one anchor and a maintenance pass then rewrote
  many pages, anchors among them and possibly the user's own page, within a
  few seconds
- **THEN** a referential turn resolves neither the page the batch wrote last
  nor the freshest page the batch left alone, however old
- **AND** it resolves the anchor the usage snapshot shows was read, or
  abstains `unresolved` with its recent-context block when nothing was read

#### Scenario: A previous packet's answer is resumed whole

- **WHEN** a referential turn that names nothing passes the continuity token of a
  packet that resolved two anchors together
- **THEN** both anchors resolve on recency and continuity, whichever was edited
  last

### Requirement: Interactive activation does bounded work under a deadline on every door

An activation request SHALL stop starting work once its request deadline can no
longer afford another stage. In a managed runtime, when the door binds no request
deadline, activation SHALL bind its own for the duration of the call and restore
the prior state afterwards, so every door of a server that can outlive its client
is bounded. An unmanaged call, whose process ends with its caller, SHALL bind
none. Each stage SHALL start only when a named reserve remains. A request that
exhausts its deadline SHALL return an unavailable abstention that discloses
nothing, names the first stage it did not run and carries its timings; it SHALL
NOT return a partially compiled packet, and SHALL NOT create a reusable packet
cache entry for one. Every abstention,
including disabled, unresolved, warming and unavailable outcomes, SHALL carry
its timings.

In a managed runtime, activation SHALL NOT build the private-identity inventory
on the request thread: while that inventory is cold it SHALL abstain as warming
and SHALL itself schedule the background build, so activation traffic alone
converges it. Activation SHALL abstain as warming for recall only while a
background owner has announced that it is seeding that scope, and SHALL NOT then
project recall freshness or walk the vault on the request thread. Where no owner
has announced a seed, or an announced seed concluded without making recall live,
activation SHALL proceed exactly as unmanaged activation does. A warming
abstention SHALL NOT depend on work that nothing is doing. A live
recall-policy mismatch SHALL fail fast through the existing live checkpoint
rather than reproject synchronously. Freshness that cannot be established SHALL
produce an unavailable abstention; later stages SHALL NOT re-attempt it.
Unmanaged and offline activation SHALL be unchanged.

The current-state lookup SHALL govern only the rows it returns. It SHALL NOT
build the vault-wide link candidate index on the request thread; a bare-title or
memory-reference link whose resolution would need that index SHALL take the
existing withheld state, and a path-shaped link SHALL keep resolving and being
authorized. A governance step SHALL be skipped only through an explicit internal
option that no tool surface can set, never by inference from caller-supplied
query arguments.

A warm activation request SHALL NOT enumerate the vault, and SHALL resolve the
vault's machine-local state location at most once. Both bound the same cost:
every system call a request makes releases the interpreter lock and waits to
reacquire it, so repeated path resolution is as expensive as a directory sweep
when the process is busy. Resolutions memoised for a request SHALL be
placement answers only — where state lives, given a vault path and the
environment that selects the state root — and SHALL NOT include anything read
as evidence, such as a file's stat signature or whether a directory exists.
The memo SHALL NOT outlive the request, SHALL NOT be shared between requests
or threads, and SHALL NOT retain a failed resolution.

The set of collection
manifests activation reads SHALL be produced where the derived index already
discovers collections, off the request path, and published for request threads
against the identity of the derived index that produced it; a request for which
nothing is published SHALL compute the set once, publish it and proceed, so a
caller that never had an index is unchanged. A set published against one
derived index SHALL NOT be served against another. The manifests used as RESOLUTION EVIDENCE MAY be as stale as
the index, exactly as anchors are. Governed current state SHALL NOT be resolved
against a stale manifest: it SHALL take only the manifest paths from that
published set and SHALL re-read the manifest of each collection it queries on
the request that queries it, so a governance-relevant manifest edit is honoured
on the next request with no index update in between. A manifest that has
vanished, no longer parses, or no longer declares the queried profile SHALL
yield no current-state rows for that collection, and the packet SHALL still
serve.

#### Scenario: Every shipped client has given up on the request

- **WHEN** an activation arrives at a managed service through a door that binds no request deadline and its work outlasts the activation door budget
- **THEN** the next stage boundary abstains as unavailable, names the stage it skipped and returns the timings of the stages that ran
- **AND** no packet content is disclosed and no packet cache entry is created

#### Scenario: A managed service has just started

- **WHEN** an activation arrives while the file watcher is seeding recall or while the private-identity inventory is cold
- **THEN** it abstains as warming without projecting freshness or walking the vault on the request thread
- **AND** the missing state is built once in the background for all concurrent callers
- **AND** a later activation serves once that work has concluded

#### Scenario: A managed service runs without a file watcher

- **WHEN** an activation arrives at a managed service whose file watcher is disabled and recall has never gone live
- **THEN** it serves through the same path unmanaged activation takes, under its request deadline
- **AND** it never reports warming for recall

#### Scenario: A warm request compiles a packet about a Records-anchored resource

- **WHEN** activation serves a turn whose anchors resolve against an index that is already current
- **THEN** it enumerates no directory other than the storage of a collection its own answer reads
- **AND** it runs no collection discovery sweep, taking the manifests the index published instead
- **AND** it resolves the vault's state location at most once, however many components ask for it
- **AND** nothing it resolved is remembered once the request ends

#### Scenario: A collection is added between two index updates

- **WHEN** a collection is added to the vault and activation serves a turn before any index update has discovered it
- **THEN** that collection contributes no resolution evidence and no current state
- **AND** it contributes both once the next index update has discovered it

#### Scenario: A collection manifest is edited without an index update

- **WHEN** a manifest is edited so that a governed query may no longer run against its collection, and activation serves the next turn before any index update
- **THEN** current state reads that manifest afresh and the query does not run
- **AND** a manifest that has vanished likewise yields no rows for that collection while the rest of the packet still serves

#### Scenario: A current-state record carries an unrelated bare-title link

- **WHEN** the newest record of a stateful collection holds a scalar state field and an unrelated link written as a bare title
- **THEN** current state reports the scalar statement and governs exactly that row
- **AND** no vault-wide candidate index is built and the bare-title link is withheld exactly as a missing target would be

### Requirement: Integration acceptance does not substitute scorer fixtures for product behaviour

The existing benchmark SHALL retain its thresholds and negative controls while exercising normal capture writers, canonical layouts, index publication and the actual compiler. Scorer unit tests and oracle packets SHALL be labelled as instrument tests. Public fixtures SHALL be synthetic; private originals SHALL remain outside public artifacts. Paid comparative experiments SHALL remain separately authorized and SHALL NOT gate this programme's deterministic and ordinary-use acceptance.

#### Scenario: An oracle packet passes but the index is empty

- **WHEN** a hand-built packet passes scoring while the corpus has not produced the expected entities, collections and graph state through the product path
- **THEN** scorer correctness may pass but compiler and integrated-loop acceptance fail

### Requirement: Cross-client activity is a deferred bounded context input

The programme SHALL retain shared activity awareness as a follow-on after the existing compiler, client acceptance, adaptation and consolidation tranches. Earlier compiler deliveries, candidate iteration and hosted launch SHALL NOT depend on enabling it. Its implementation SHALL first assess reuse of current host/harness activity, session, handoff and continuity mechanisms rather than assume a new channel is necessary. Native clients SHALL retain execution ownership.

When available, relevant activity SHALL be a separately labelled, bounded operational input to context activation. Records SHALL carry a scoped activity identity, bounded subject/action, authenticated reporter with attributable client/session identity, report revision, reported status, service-observed contact time and authorized result/handoff references. The implementation SHALL define finite publication, retention and read budgets. Both reports and linked references SHALL obey current user/workspace/audience disclosure boundaries. Activity SHALL remain within the existing total activation token/latency limits and a finite activity sub-budget, with explicit omission and availability information. Disabled or unavailable activity SHALL NOT prevent normal capture or activation.

#### Scenario: Another participating client reports related work

- **WHEN** a supported client begins a coding or non-coding task overlapping a current authorized activity report
- **THEN** activation supplies the relevant attributed report within its existing total budget without requiring a coordination reminder
- **AND** the active agent can distinguish reported overlap from verified external state and from similar but independent tasks
- **AND** the report grants no permission to contact, stop or take over the other session

#### Scenario: The feed is absent or outside the reader's scope

- **WHEN** the activity feed is disabled, unavailable, truncated or lacks participation from another client
- **THEN** activation reports the applicable limitation without claiming that no work exists
- **AND** capture and durable context activation remain usable
- **AND** unauthorized activity and linked-result contents are not disclosed, including after access revocation

### Requirement: Activity freshness does not establish progress or exclusivity

Activity reports SHALL distinguish reported status, contact freshness and independently verified result evidence. Expired contact SHALL mean stale or unknown, never completion or permission to assume ownership. Replayed or out-of-order updates SHALL NOT refresh or resurrect stale or terminal reports. A heartbeat SHALL NOT establish progress. Conflicting advisory claims SHALL remain attributable and SHALL NOT be silently resolved by arrival order or semantic similarity. The system SHALL expose participation coverage rather than assume every MCP client reports activity.

Presence and advisory ownership SHALL remain transient operational state. Supported durable outcomes MAY enter the episode compiler through existing governed writers and evidence rules; presence SHALL NOT count as independent knowledge evidence or episode completion. Advisory activity SHALL NOT authorize effects or guarantee duplicate-effect prevention. Any exclusive-action claim SHALL require a separately reviewed atomic ownership and execution-side fencing/idempotency contract at the side-effect owner, rather than treating the storage writer lease as task ownership.

#### Scenario: A client stops reporting midway through work

- **WHEN** the last contact expires or an old active update arrives after expiry or a terminal report
- **THEN** the view preserves stale/unknown or the newer terminal report as applicable instead of inventing progress or completion
- **AND** neither expiry nor replay authorizes a retry, takeover or duplicate side effect

#### Scenario: Completion is reported without a verified result

- **WHEN** a client marks work complete but its result evidence is unavailable or unverified
- **THEN** context labels completion as the client's report and retains the evidence limitation
- **AND** neither an episode's reconciled completion nor a verified durable outcome is inferred from that report

#### Scenario: Two clients claim the same action

- **WHEN** two attributable activity records claim responsibility for overlapping work
- **THEN** bounded context exposes the advisory conflict without choosing an exclusive owner or automatically suppressing either task
- **AND** any actual side effect remains subject to its existing authorization, idempotency and execution boundaries
