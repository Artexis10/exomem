## ADDED Requirements

### Requirement: Optional Preloads Do Not Hold Retrieval Readiness

The managed warm SHALL mark its required stages finished — the retrieval catalogue, the graph handoff, the semantic corpus and the lexical caches — before any optional model preload begins. From that point, retrieval admission that was revoked or never granted SHALL be re-proved exactly as it is once the whole warm has finished, as required by "Retrieval Readiness Recovers After Repair", by the readiness probe and by a recall request refused for want of admission. An optional model preload MUST NOT delay retrieval readiness, and it SHALL continue in the background. A failing proof SHALL still fail closed. The readiness probe SHALL run its proof off the serving event loop, so a slow proof cannot stall liveness probes or requests.

#### Scenario: A slow optional preload does not hold readiness

- **WHEN** the required warm stages have finished and the reranker preload is still loading
- **AND** retrieval admission was revoked during the warm and the catalogue proof now holds
- **THEN** the next readiness probe re-proves the catalogue and reports ready
- **AND** the reranker preload continues in the background

#### Scenario: Admission is not re-proved before the required stages finish

- **WHEN** retrieval admission is revoked while the required warm stages are still running
- **THEN** retrieval is reported as warming until those stages finish or the catalogue repair publishes

#### Scenario: A failing proof after the required stages still fails closed

- **WHEN** the required warm stages have finished and the catalogue proof does not hold
- **THEN** retrieval stays unadmitted
- **AND** it is admitted once a later proof holds, without a process restart

#### Scenario: A slow readiness proof does not stall liveness

- **WHEN** a readiness probe's proof waits on a lock
- **THEN** liveness probes and requests on the same worker are answered meanwhile
