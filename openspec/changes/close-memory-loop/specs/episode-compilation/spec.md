## Purpose

Compile substantive conversational episodes into complete, supported durable changes with canonical routing, bounded execution and truthful interruption recovery.

## ADDED Requirements

### Requirement: Episode decomposition precedes destination selection

At a supported substantive episode boundary the active agent SHALL consider the original input for durable observations, outcomes, entity creation/hydration, facets, relationships, Records events, expressed Planning changes, Source/Evidence preservation and structural-routing candidates before choosing write destinations. Each candidate SHALL be resolved against current knowledge and receive an attributable disposition. Capture sweep SHALL NOT be the sole trigger or completeness check. No-op, uncertain, rejected, deferred and awaiting-authority outcomes SHALL be legitimate and distinct.

#### Scenario: A rich episode exceeds the first note's scope

- **WHEN** the initial note can hold only part of an episode's supported durable changes
- **THEN** decomposition still considers the remaining changes and resolves their separate destinations
- **AND** an incidental name or uncertain ownership claim may be rejected or deferred without manufacturing an entity or edge

### Requirement: Canonical destination fitness is distinct from similarity

Before committing a substantive candidate, the active agent SHALL compare its proposed canonical home with bounded inspected alternatives. The comparison SHALL consider title/scope alignment, artifact role, independent meaning, future lookup language, consequential facets, supported relationship fan-out, synthesis versus refinement and continuation value as applicable. Retrieval similarity, a stable name, page length or an edge count SHALL NOT independently determine destination. The active agent SHALL remain the semantic decider; deterministic infrastructure SHALL NOT score meaning or promote concepts autonomously.

The episode plan SHALL distinguish updating an existing page, adding a semantic unit, creating a focused note, creating/hydrating an entity, routing by Records/Planning/experiment role, preserving Source/Evidence, adding only a relationship and no durable capture. It SHALL retain the selected route, candidate/input revision, target identity or proposed title, bounded alternative scope/version evidence and an attributable reason. Missing candidate evidence SHALL be explicit. Destination preparation SHALL bind current leaf targets and preserve their existing authority and stale-target checks. Replanning SHALL preserve candidate-to-leaf reconciliation.

#### Scenario: A named synthesis spans related narrower pages

- **WHEN** an ordinary episode develops an independently useful named thesis synthesizing several existing concepts
- **THEN** the agent inspects the related pages and chooses a focused first-class home on the first capture pass when their narrower scopes do not own that thesis
- **AND** truthful typed relations connect the antecedents while narrower pages receive only scope-owned updates
- **AND** semantic similarity alone does not authorize appending the thesis to the nearest page

#### Scenario: A minor refinement belongs to its existing home

- **WHEN** a new detail refines an existing concept without independently useful scope or continuation value
- **THEN** the agent updates the existing home or adds a semantic unit there
- **AND** shared vocabulary, a newly coined label or a desire for more links does not manufacture another page

#### Scenario: A prepared destination changes before commit

- **WHEN** the selected target or decision-relevant evidence changes after preparation
- **THEN** the existing target validation requires fresh consideration or proves the identical binding remains valid
- **AND** the prior candidate and attempted leaf identities are retained rather than silently retargeted or retried as new work

### Requirement: Destination coverage is reviewed before effects

Before the first canonical effect, the active agent SHALL review the bounded plan against its original input revision for omitted durable candidates and unsuitable destinations. Every identified candidate SHALL have a supported routing, abstention, deferral or awaiting-authority disposition. This review SHALL be recorded separately from post-write receipt/readback reconciliation and SHALL NOT confer write authority or require a redundant user confirmation. Deferred work SHALL remain pending where appropriate. The server SHALL validate the recorded contract without claiming to prove semantic completeness or destination quality.

#### Scenario: All facts are present but the thesis has the wrong home

- **WHEN** a proposal covers every identified fact by appending them to a related page whose title/scope does not own their independently useful synthesis
- **THEN** the precommit agent review revises the destination plan before canonical writes
- **AND** successful leaf validation alone does not establish destination fitness or completed coverage

### Requirement: Input evidence and revisions support recovery

Episode state SHALL bind a logical episode identity, input revision and original evidence references or minimal authorized excerpts sufficient to resume the candidate decisions. It SHALL obey disclosure and retention policy and SHALL NOT require full-transcript persistence by default. A digest without recoverable evidence SHALL NOT count as preserved original input. Missing evidence SHALL produce an explicit unavailable recovery state. User corrections SHALL create a new input revision requiring reconsideration of affected candidates.

#### Scenario: Conversation context disappears after a partial write

- **WHEN** an episode resumes after context loss
- **THEN** retained authorized evidence supports the remaining decisions or recovery reports unavailable
- **AND** the system does not reconstruct purported original facts from an unsupported summary or hash

#### Scenario: Two sessions advance the same episode

- **WHEN** two authorized episode owners submit transitions from the same stored revision and digest
- **THEN** only one can advance that revision and the other must reload the accepted history
- **AND** persisted intent is bounded, reconstructed through validated transitions and cannot accept arbitrary executable payloads or caller-supplied completion proofs

#### Scenario: One owner resumes through a different client

- **WHEN** the same resolved canonical audience resumes an episode through another supported transport or session
- **THEN** its logical episode and accepted history remain available subject to current evidence authorization
- **AND** a different audience using the same episode key cannot read or change that history
- **AND** absent identity fails closed and ownerless legacy history is not adopted implicitly

#### Scenario: A retained input changes or becomes inaccessible

- **WHEN** recovery resolves a stored page or exact semantic-unit reference
- **THEN** it binds disclosure and the recorded version to the same immutable current snapshot
- **AND** changed evidence is stale, while missing, ambiguous, withheld or superseded evidence is unavailable, without returning replacement text
- **AND** a response budget cannot silently truncate input and still classify it as complete recovery
- **AND** the response excludes raw journal events, sealed execution plans and unrestricted candidate payloads

#### Scenario: Conversation input was not retained canonically

- **WHEN** the host has only a digest of vanished conversation input
- **THEN** recovery remains unavailable and the episode layer does not implicitly persist a transcript
- **AND** canonical Source/Evidence retention continues through its existing authorized writer without a new confirmation or permission grant
- **AND** recovering an artifact companion page alone never claims recovery of the original artifact bytes

#### Scenario: Input prose names a target through its title or alias

- **WHEN** recovered input references another page through a title, alias or equivalent normalized spelling
- **THEN** recovery checks every resolved binding against current disclosure policy using current maintained lookup evidence
- **AND** a withheld binding or unavailable lookup leaves recovery unavailable without exposing the input text
- **AND** merely checking a linked target does not record its unreturned content as disclosed

#### Scenario: Accepted commitment survives later content changes

- **WHEN** an episode has durably recorded a receipt-verified commit and its note is subsequently edited
- **THEN** reconstruction retains that historical commitment without rerunning a writer or treating it as permission to retry
- **AND** historical coverage is distinguished from current coverage, which requires fresh verification before another postcommit attestation
- **AND** a current readback mismatch does not erase independent pending candidates

### Requirement: Candidate identity is independent of proposal revision

Candidate identity SHALL remain stable across destination re-resolution, proposal revisions and reordered plans. Existing mutation receipts and curation execution SHALL remain the leaf execution authority. The episode coordinator SHALL retain candidate-to-operation mappings and reconcile prior attempted effects before assigning or retrying equivalent work. A changed semantic effect SHALL require a recorded revision and current validation/authority. Uncertain outcomes SHALL remain bound pending reconciliation rather than being retried with a fresh identity.

#### Scenario: A plan is reordered after one committed leaf

- **WHEN** an interrupted episode is re-planned with a different step order or proposal identity
- **THEN** its committed effect resolves to the original receipt and is not executed again
- **AND** only remaining currently authorized effects may proceed

#### Scenario: A leaf result is reconciled from stored evidence

- **WHEN** an owner-held episode leaf has an uncertain attempted outcome
- **THEN** the recovery adapter verifies its exact stored plan, approval, operation, receipt, atomic witness and applicable current readback before recording a verified committed outcome
- **AND** caller-supplied success flags, mutable progress projections and unmatched evidence cannot substitute for that proof
- **AND** reconciliation itself performs no canonical write or leaf execution
- **AND** the existing consistency boundary excludes canonical mutations throughout the evidence read without granting writer authority

#### Scenario: A missing or historical result cannot authorize a retry

- **WHEN** a terminal receipt is missing, a committed postimage has changed, or only an earlier failed attempt is evidenced
- **THEN** the episode remains uncertain until the existing execution owner and appropriate evidence contract reconcile the result
- **AND** absence, failure or stale readback is never treated as proof that the current attempt did not commit

### Requirement: Completion attests coverage of original input

The episode ledger SHALL distinguish attempted work, pending continuation and coverage through an input revision. Completion SHALL require an active-agent pass against that original revision, including corrections, plus receipt/readback reconciliation of claimed effects. A successful write, saved marker, short assistant response or cooldown SHALL NOT independently establish completeness. The server SHALL validate recorded state transitions without claiming it can prove semantic exhaustiveness.

#### Scenario: One write succeeds while other candidates remain

- **WHEN** a capture write commits but the original episode contains unresolved durable changes
- **THEN** the episode remains pending and its host checkpoint cannot classify it complete solely because a write succeeded

#### Scenario: A correction arrives after coverage was recorded

- **WHEN** the user changes the evidence or a claim in a later episode input revision
- **THEN** prior coverage remains historical and affected candidates require reconciliation before coverage advances

### Requirement: Fan-out does not manufacture independent recurrence

Source/episode/span origin identity SHALL propagate through every compiled destination, recurrence detector, write-time carrier and relevant relation/hydration evidence. Compilation SHALL NOT increase the number of independently established input origins. Conversation-only fan-out SHALL inherit one episode origin; independently established original sources discussed in that episode SHALL retain their distinct identities. Copy-equivalent projections SHALL add no independent origin. If independence cannot be determined, evidence SHALL be labelled unassessed rather than treated as independent page mentions. Supported important first mentions SHALL remain independently eligible for semantic promotion.

#### Scenario: One turn becomes four pages

- **WHEN** a single episode is preserved as a Source, entity facet, focused note and Records item
- **THEN** those four destinations contribute one origin to recurrence pressure rather than four independent mentions

#### Scenario: One episode discusses two independent original sources

- **WHEN** an episode uses two sources whose independence is established by provenance
- **THEN** their compiled outputs retain those two source origins
- **AND** copying either source into additional destinations contributes no further independent origin

### Requirement: Bounded partial work resumes honestly

Episode work SHALL have explicit per-pass resource bounds, durable continuation and publication status. At the next eligible lifecycle boundary, supported hosts SHALL offer pending work to the active agent without reapplying committed leaves. Tool-only clients SHALL expose the same resume contract with best-effort initiation. Expired authority, unavailable evidence or stale targets SHALL pause affected steps with a reason; unrelated committed effects remain durable. The product SHALL state that unseen or unpersisted pre-crash input is outside recovery coverage.

#### Scenario: The budget expires before graph publication

- **WHEN** canonical writes commit but projection publication or remaining candidates exceed the pass budget
- **THEN** the receipt reports durable effects and explicit pending work/publication
- **AND** the next eligible pass resumes within bounds rather than claiming a fully current graph
