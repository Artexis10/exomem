## ADDED Requirements

### Requirement: A structural suggestion resolves when the corpus gives its material a home

When the material a structural suggestion describes already has eligible compiled destinations declaring its vocabulary, the system SHALL NOT emit the suggestion, and it SHALL reach that outcome from corpus state alone.

The system SHALL NOT record, read, or rely on any acceptance, dismissal, snooze, cooldown, or per-page suggestion history to reach this outcome. Removing the destinations SHALL restore the suggestion, because nothing about the earlier emission was retained.

A destination SHALL contribute only when it is a compiled page eligible for the caller's own recall, is not the page just written, and its declared identity covers at least two of the cluster's recurring terms. Resolution SHALL be evaluated by removing the covered terms and re-applying the existing mass requirement, so a partially routed cluster still produces a suggestion for the part that has no home.

Resolution SHALL be expressed only as the absence of a suggestion. It SHALL NOT add a key, a reason code, a destination name, a path, a count of destinations, or any other fact about a page other than the one written.

#### Scenario: A suggestion acted on by creating destinations stops firing

- **WHEN** a compiled page carries a recurring off-scope cluster that would otherwise produce a suggestion
- **AND** the vault contains eligible compiled destinations whose declared identities together cover that cluster's recurring terms
- **AND** a further compiled write to the original page commits
- **THEN** the response contains no `structure_suggestion` key
- **AND** the original page's own durable units are unchanged, having been neither removed nor rewritten

#### Scenario: Resolution survives no dismissal record and reverses with the destinations

- **WHEN** the destinations that resolved a cluster are removed from the corpus
- **AND** a further compiled write to the original page commits
- **THEN** the suggestion is emitted again with its existing shape
- **AND** no stored acceptance or dismissal was consulted to reach either outcome

#### Scenario: An incidental single-term match cannot silence a cluster

- **WHEN** the only pages declaring any of a cluster's terms each cover exactly one of them
- **THEN** no destination contributes
- **AND** the suggestion is emitted unchanged

#### Scenario: A partially routed cluster still reports the unrouted remainder

- **WHEN** eligible destinations cover only part of a cluster's recurring terms
- **AND** the remaining terms still satisfy the existing mass requirement
- **THEN** a suggestion is emitted

### Requirement: Structural resolution reads only corpus state the write already holds and fails open

The system SHALL evaluate resolution using the corpus context the mutation already built. It SHALL NOT perform a corpus walk, index read, database query, embedding, or model call for this purpose, and it SHALL NOT build a second corpus context.

The destination set SHALL be drawn from the pages eligible for the caller's own recall, so a page the caller is not entitled to see SHALL NOT affect the caller's suggestion.

When no corpus context is available, or resolution cannot be evaluated for any reason, the system SHALL emit the suggestion exactly as it does without resolution. Suppression SHALL NEVER be the fallback behaviour, and a failure in this analysis SHALL NEVER affect the committed write, its terminal, its status, or its replay behaviour.

#### Scenario: Every compiled writer evaluates resolution

- **WHEN** a page whose cluster is fully routed is mutated through `remember`, `edit_memory`, `observe_memory`, or `replace_memory`
- **THEN** none of the four responses carries a `structure_suggestion`

#### Scenario: An unavailable corpus emits rather than suppresses

- **WHEN** resolution cannot be evaluated because no corpus context is available
- **THEN** the suggestion is emitted with its existing shape
- **AND** the committed write, its terminal, and its status are unchanged

#### Scenario: An ineligible destination does not resolve a cluster

- **WHEN** the only page declaring a cluster's vocabulary is not eligible for the caller's own recall
- **THEN** that page does not contribute
- **AND** the suggestion is emitted unchanged
