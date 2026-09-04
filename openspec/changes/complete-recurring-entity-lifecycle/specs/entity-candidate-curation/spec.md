## ADDED Requirements

### Requirement: One bounded candidate represents each recurring identity
The system SHALL project at most one open entity candidate per normalised identity. A candidate SHALL preserve the identity key, deterministic display form, candidate state, full recurrence counts, bounded contributing page and origin references, bounded material facet atoms, role or membership evidence, co-occurring resolved entities, active and unresolved type cues, bounded resolution candidates, grammar version, predicate-table digest, and the entity-type registry identity. The contract SHALL be identical for every core or vault-defined kind.

#### Scenario: Different identity kinds share one payload
- **WHEN** qualifying evidence describes a person, community, organisation, place, product, venue, account, project, or vault-defined kind
- **THEN** every candidate uses the same state and evidence schema
- **AND** no kind-specific detector is required for eligibility

#### Scenario: Evidence samples are bounded without hiding totals
- **WHEN** an identity has more contexts, facets, or near matches than the response bounds
- **THEN** the candidate returns deterministic bounded samples, full counts, and explicit truncation
- **AND** ordering is independent of insertion order

#### Scenario: Detector identity is inspectable
- **WHEN** an ordinary-text candidate is returned
- **THEN** its evidence identifies `identity-frames-v1`, the frozen predicate-table digest, and the registry fingerprint
- **AND** no unversioned or plugin-added cue can contribute

### Requirement: Candidate state is promotion, hydration, or ambiguity
The system SHALL resolve candidate state deterministically. No active Entity match SHALL produce `promotion`; exactly one active exact or alias match with disconnected qualifying evidence SHALL produce `hydration`; several active matches or incompatible deterministic evidence clusters SHALL produce `ambiguous`. Near matches and type cues SHALL remain advisory and MUST NOT select a target.

#### Scenario: Missing Entity routes to promotion
- **WHEN** a qualifying identity resolves to no active Entity
- **THEN** its candidate state is `promotion`
- **AND** existing similarly titled non-Entity pages and near matches remain evidence rather than silently becoming targets

#### Scenario: Existing Entity routes to hydration
- **WHEN** one active Entity resolves the identity and qualifying contexts lack a canonical link or accepted relation to it
- **THEN** its candidate state is `hydration`
- **AND** the resolved Entity ref and disconnected contexts are bound in the candidate

#### Scenario: Several matches route to ambiguity
- **WHEN** exact or alias resolution returns several active Entities
- **THEN** the candidate state is `ambiguous`
- **AND** neither promotion nor hydration is executable until resolution changes

### Requirement: Candidate lifecycle closes and reopens from state
A candidate SHALL close when its qualifying evidence falls below the gate or its state-specific defect is repaired. Promotion SHALL transition to hydration when an Entity is created but qualifying contexts remain disconnected. Hydration SHALL expose a deterministic first batch of at most eight contexts, `remaining_disconnected_count`, and `batch_fingerprint`, and SHALL close only when every qualifying context is connected, removed, or becomes ineligible. Ambiguity SHALL transition according to the remaining resolution after reconciliation. Deleting a target or connection SHALL restore the corresponding state without requiring dismissal changes.

One confirmed hydration plan SHALL bind and act on only its returned batch. After a terminal mutation receipt, exactly one identity-bound recurrence recheck SHALL recompute the full corpus. After processed batches 1–7, that recheck MAY expose the next deterministic batch with a new signal version while disconnected contexts remain. After processed batch 8, the recheck SHALL be closure-only and SHALL expose only closed or a bounded `deferred_remaining_count`, never a ninth batch or plan binding. Each exposed batch SHALL require a separately authored plan and exact confirmation; no prior confirmation SHALL authorize a later batch. The chain SHALL stop on any non-terminal outcome, state or target change, refusal/dismissal, zero remaining count, or the eighth closure-only recheck. At most eight batches are mutated and eight identity rechecks run in one session. Any remainder SHALL stay open and resume from the next session's ordinary recurrence read.

#### Scenario: Creation transitions promotion to hydration
- **GIVEN** an open promotion candidate
- **WHEN** exactly one active Entity is created for the identity while qualifying contexts remain disconnected
- **THEN** the same identity projects one hydration candidate instead of a duplicate promotion

#### Scenario: Connecting evidence closes hydration
- **GIVEN** an open hydration candidate
- **WHEN** every qualifying context gains a canonical Entity link or accepted graph relation
- **THEN** no candidate remains on the next pass
- **AND** removing a connection restores hydration from corpus state

#### Scenario: More-than-bound hydration converges by recheck
- **GIVEN** an Entity has more disconnected qualifying contexts than one candidate batch can carry
- **WHEN** the agent separately confirms each returned batch, observes its terminal mutation receipt, and performs exactly one identity-bound recheck
- **THEN** each recomputation returns the next sorted unconnected batch without duplicates or starvation
- **AND** hydration closes only after the recomputed remaining count reaches zero, continuing in a later ordinary session if the eight-batch session budget is reached

#### Scenario: Ninth batch is deferred exactly
- **GIVEN** an ordinary session begins with enough disconnected contexts for at least nine batches
- **WHEN** eight batches are separately confirmed and reach terminal mutation receipts
- **THEN** exactly eight identity rechecks have run, the eighth reports `deferred_remaining_count`, and no ninth batch or plan binding is exposed
- **AND** the next bootstrapped session's ordinary recurrence read may expose batch nine

#### Scenario: Later hydration batch needs fresh confirmation
- **GIVEN** one hydration batch completed and the next recheck exposes remaining contexts
- **WHEN** no new exact confirmation is supplied
- **THEN** no context in the new batch is mutated

### Requirement: Review identity is stable and material-change sensitive
Entity candidates SHALL use an identity partition. Their signal version SHALL bind candidate state, grammar version, predicate-table digest, sorted material facet hashes, sorted hashes of every disconnected qualifying context including those outside the returned batch, resolved target refs, material type-family evidence, and registry identity. Redundant mentions, ordering, punctuation, and another copy of an existing facet MUST NOT change the signal version. A distinct new facet, connection change, completed hydration batch, resolution transition, ambiguity change, target deletion, or relevant registry change SHALL change it.

#### Scenario: Redundant recurrence respects dismissal
- **WHEN** a dismissed candidate receives another occurrence of an already-recorded facet
- **THEN** its signal version is unchanged and the dismissal remains effective

#### Scenario: Material facet reopens review
- **WHEN** a dismissed identity gains a distinct qualifying facet in another independent origin
- **THEN** its signal version changes and the item is eligible to surface again

### Requirement: Candidate action remains active-agent authored and governed
After the `add-governed-curation-lane` prerequisite is shipped, the curation work-item action SHALL accept an exact entity-candidate review ref and return its bounded evidence and current bindings. Exomem MUST NOT author a plan, choose an Entity, choose a type, infer a merge, or mutate content. Promotion and hydration plans SHALL use only the governed curation lane's closed step kinds and exact approval contract. Ambiguity SHALL have no default executable plan. Registering an unknown kind SHALL remain a separate guarded entity-type registry save.

#### Scenario: Agent opens a promotion work item
- **WHEN** the active agent requests a work item for a current promotion candidate
- **THEN** Exomem returns measured evidence, exact bindings, registry identity, and allowed curation step schemas
- **AND** it does not create a plan or Entity

#### Scenario: Agent proposes hydration
- **WHEN** the active agent authors a hydration plan using guarded `edit` or `accept-relation` steps
- **THEN** proposal, preview, approval, apply, receipts, recovery, and compensation use the existing governed curation contract

#### Scenario: Curation prerequisite is not papered over
- **WHEN** governed curation is not yet implemented and independently verified
- **THEN** detector and candidate reads may operate but candidate-to-plan integration is not reported as available

#### Scenario: Candidate changed before proposal
- **WHEN** the candidate signal version, resolved Entity, context hash, or registry identity changed after review
- **THEN** proposal creation refuses stale and the agent must request a fresh work item
