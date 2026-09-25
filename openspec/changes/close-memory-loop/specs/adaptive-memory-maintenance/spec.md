## Purpose

Keep working context and knowledge organisation useful as the vault evolves through governed corrections, derived profiles and bounded consolidation proposals.

## ADDED Requirements

### Requirement: Adaptation is evidence-bound and governed

Corrections and observed capture/activation misses SHALL produce reviewable evidence-bound candidates for registered roles, cues, aliases, conventions or other supported definitions. The active agent SHALL decide semantics under the applicable typed writer and authority, preserving the current context-role owner-authored override gate. Accepted changes SHALL be versioned, reversible and visible to fresh sessions. Product code SHALL NOT hardcode private entities, suppliers, domains or task phrases. Adaptation SHALL NOT change identity, provenance, authority or abstention invariants, or infer an unregistered authority action from an additive grant.

#### Scenario: A correction improves the next session

- **WHEN** an authorized correction revises an applicable context role or vault convention
- **THEN** a fresh session consumes its current version and the previous version remains attributable and recoverable
- **AND** the same operation without appropriate authority cannot silently edit the definition

### Requirement: Hot profiles remain bounded derived projections

The system SHALL provide a compact hot profile derived from authorized canonical knowledge with source provenance, explicit budgets and currency. Corrections, expiry, deletion, supersession and access changes SHALL invalidate affected profile material. Missing or stale profiles SHALL report their state and SHALL NOT become an alternate canonical store or bypass disclosure checks.

#### Scenario: A profiled preference is corrected or hidden

- **WHEN** its canonical source changes or current access no longer permits disclosure
- **THEN** subsequent packets omit or refresh the affected profile entry and never serve the stale value as current

### Requirement: Priors cannot override grounded resolution

Activation priors SHALL be bounded derived ranking signals with provenance, versioning and invalidation. They SHALL NOT create facts, resolve ambiguous identity without evidence, override explicit task anchors or promote superseded state. Acceptance SHALL measure usefulness and latency alongside rare-anchor and popularity-trap negatives.

#### Scenario: Frequent history is irrelevant to the current task

- **WHEN** a high-frequency prior conflicts with a resolved explicit task anchor
- **THEN** grounded task relevance wins and irrelevant history cannot consume the entire context budget

### Requirement: Dreamer proposes bounded consolidation off the interactive path

The dreamer SHALL provide deterministic, delta-driven or idle-scheduled consolidation proposals using bounded indexed evidence. Candidate families SHALL include supported alias/anchor, category/convention, link, hydration and profile improvements. The active agent SHALL remain the semantic decider and canonical writers SHALL enforce current authority. Background execution SHALL be default-off and provide pause/quiet controls, explicit work/time/memory bounds, checkpointed continuation, evidence-version invalidation and deduplication. It SHALL NOT perform autonomous canonical writes or be required for online capture/recall.

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

Any frozen verifier SHALL obey the existing canonical `frozen-verifiers` admission and effects requirements, remain default-off, version-pinned, separately admitted and limited to review labels with abstention. It SHALL NOT author knowledge, select canonical identity, control retrieval/ranking, gate capture or define policy. Failure, abstention or resource pressure SHALL remove optional assistance without changing online semantics. CPU-first admission SHALL measure quality, false positives and resource interference; GPU use SHALL require separately verified co-tenant capacity. Programme acceptance SHALL include an admission decision with evidence, not mandatory model enablement.

#### Scenario: A verifier disagrees or cannot run

- **WHEN** optional verification abstains, fails or emits a disputed label
- **THEN** the active agent can inspect original evidence and the ordinary governed workflow remains available
- **AND** the label cannot directly change a canonical fact, authority decision or retrieval result

#### Scenario: Verifier labels do not reach upkeep

- **WHEN** an admitted verifier is enabled while upkeep proposals are produced and delivered
- **THEN** no upkeep family, carrier or review surface calls the verifier, and upkeep output is identical to a run with it disabled
