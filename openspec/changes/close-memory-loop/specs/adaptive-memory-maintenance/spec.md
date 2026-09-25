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

The dreamer SHALL provide deterministic, delta-driven or idle-scheduled consolidation proposals using bounded indexed evidence. Candidate families SHALL include supported alias/anchor, category/convention, link, hydration and profile improvements. The active agent SHALL remain the semantic decider and canonical writers SHALL enforce current authority. Background execution SHALL be default-off and provide pause/quiet controls, explicit work/time/memory bounds, checkpointed continuation, evidence-version invalidation and deduplication. It SHALL NOT perform autonomous canonical writes or be required for online capture/recall.

Eligible proposals SHALL enter the existing bounded review/activation carrier at an ordinary supported lifecycle boundary without requiring an explicit review request. Delivery SHALL respect current quiet/defer settings and existing budgets. Tool-only clients SHALL expose the same proposals with best-effort initiation. Acceptance SHALL establish next-session delivery and authorized agent disposition, not only queue creation.

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

### Requirement: Frozen verifiers are optional review labels only

Any frozen verifier SHALL obey the existing canonical `frozen-verifiers` admission and effects requirements, remain default-off, version-pinned, separately admitted and limited to review labels with abstention. It SHALL NOT author knowledge, select canonical identity, control retrieval/ranking, gate capture or define policy. Failure, abstention or resource pressure SHALL remove optional assistance without changing online semantics. CPU-first admission SHALL measure quality, false positives and resource interference; GPU use SHALL require separately verified co-tenant capacity. Programme acceptance SHALL include an admission decision with evidence, not mandatory model enablement.

#### Scenario: A verifier disagrees or cannot run

- **WHEN** optional verification abstains, fails or emits a disputed label
- **THEN** the active agent can inspect original evidence and the ordinary governed workflow remains available
- **AND** the label cannot directly change a canonical fact, authority decision or retrieval result
