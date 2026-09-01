## ADDED Requirements

### Requirement: Portable carriers deliver the bounded recurring-identity check
The generic scaffold, installed core skill, and hookless custom-instructions projection SHALL carry the same capability-honest recurrence cadence as bootstrap. At balanced and maximal prominence, an active agent SHALL request the explicit `entity_recurrence` category at most once ordinarily per bootstrapped conversation or session during the first user turn after successful bootstrap, after primary task work and before the final response, including when that interaction is not entity-topical. It MAY perform exactly one additional general state recheck after an Entity, accepted relation, or entity-type registry mutation outside hydration continuation. Each ordinary or general read SHALL request one audit pass and at most three candidates.

After each separately confirmed hydration batch returns a terminal mutation receipt, the agent MAY perform exactly one immediate recheck bound to the same identity. Rechecks after processed batches 1–7 may expose at most the next eight contexts; the eighth recheck after processed batch 8 is closure-only and exposes no ninth batch. Another recheck requires another fresh confirmation and terminal receipt. The chain SHALL stop on the specified state/outcome stops or after eight mutated batches and eight identity rechecks in one session; remaining work stays open for the next session's ordinary read. Off and light SHALL remain explicit-request-only.

The carriers SHALL teach resolve-before-create, ambiguity stop, hydrate-before-duplicate, and governed unknown-kind registration without listing a closed ontology. When the exported surface cannot request the category, they SHALL mark the check unavailable and skip it rather than inventing a command, local scan, model inference, or due-state result.

#### Scenario: Hookless agent checks on an ordinary interaction
- **GIVEN** a clean balanced client with only the generic portable contract and an explicit category-read surface
- **WHEN** the first normal maintenance boundary follows a non-topical ordinary user prompt
- **THEN** the agent requests `entity_recurrence` once with a limit of three and surfaces one returned candidate for semantic review
- **AND** a frequency-matched incidental twin is absent

#### Scenario: Later ordinary prompts do not rescan
- **GIVEN** the session already spent its ordinary recurrence read
- **WHEN** later prompts arrive without a qualifying mutation
- **THEN** the carrier authorizes no second recurrence scan in that session

#### Scenario: Post-mutation recheck observes lifecycle state
- **GIVEN** the ordinary recurrence read was spent and an Entity, accepted relation, or registry entry is then committed
- **WHEN** the mutation receipt is terminal
- **THEN** the agent may perform one immediate general recurrence recheck to observe closure or transition
- **AND** no second general read is authorized

#### Scenario: Hydration continuation is user-paced and session-bounded
- **GIVEN** a hydration identity has more than one batch of disconnected contexts
- **WHEN** each separately confirmed batch reaches a terminal mutation receipt
- **THEN** exactly one same-identity recheck may expose the next batch and then pauses for fresh confirmation
- **AND** after eight mutated batches the eighth recheck is closure-only and any remainder stays open for the next session rather than exposing batch nine or causing a background loop

#### Scenario: Portable carrier cannot overstate capability
- **GIVEN** a hookless client without an explicit category-read surface
- **WHEN** the recurrence boundary arrives
- **THEN** the carrier marks the check unavailable and performs no substitute scan or semantic inference
