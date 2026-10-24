# context-miss-learning Specification

## Purpose
Connect observed context failures to source-backed data corrections and later answer evidence through the existing governed agent workflow.

## Requirements

### Requirement: A miss remains linked to its evidence

A source-backed correction SHALL NOT require an incident record. When the user asks to record or learn from the miss, a workflow skill engages this workflow, or the live engagement policy delegates filing an observed outcome, the agent workflow SHALL link the observed miss, source-backed expectation, available context, correction, write receipts, and verification evidence in an existing incident collection. Missing evidence SHALL remain explicit. Collection manifests SHALL own field names and lifecycle values.

#### Scenario: The user asks only for the fix

- **WHEN** the user reports a wrong answer and asks only that the notes be fixed
- **THEN** the agent makes the source-backed correction with its reason, and files no incident unless the live engagement policy delegates it

#### Scenario: The user asks to record the miss

- **WHEN** the user asks to record the miss and an existing incident collection fits
- **THEN** the agent maps the evidence onto that collection's declared fields and records what it did not verify

#### Scenario: A failure lacks its original delivered context

- **WHEN** an incident preserves the request and source but lacks the delivered context
- **THEN** the workflow records that gap and does not claim to have located the first failed boundary

#### Scenario: A collection uses its own schema

- **WHEN** an existing incident collection declares different field names or lifecycle values
- **THEN** the agent follows that schema and reports missing fields instead of introducing product-defined values

### Requirement: Data corrections require source support

The workflow SHALL establish the failed boundary before proposing a data correction. Each proposed correction SHALL cite source evidence and current target versions. A generated answer, naming counter, or retrieval rank alone SHALL NOT establish a source fact or authorize an edit.

#### Scenario: A description omits an authored constraint

- **WHEN** source evidence supports the constraint and the captured catalogue description omits it
- **THEN** the agent can propose a supported description correction with the source and target version linked

#### Scenario: The engine loses information that the data already supplies

- **WHEN** the source and supplied context are correct but the engine or answer drops them
- **THEN** the workflow preserves that boundary and does not alter source data merely to fit the failed output

### Requirement: Corrections preserve existing write authority

The workflow SHALL use existing governed writers and their current authority, disclosure, version, and provenance rules. It SHALL preserve immutable originals and append correction evidence. An incident or advisory SHALL NOT grant new write permission, create a collection, or execute a curation plan.

#### Scenario: A target changes before the edit

- **WHEN** a proposed correction names a stale target version
- **THEN** the existing writer rejects that version and the agent rereads the target before reconsidering the correction

#### Scenario: A restricted reader reviews a miss

- **WHEN** a reader cannot access material linked by an incident
- **THEN** the served incident and proposed correction respect the same disclosure rules as their source material

### Requirement: Learning excludes held-out evaluation material

Learning SHALL use permitted operational evidence and development cases only. Held-out inputs, truth keys, arm maps, and generated held-out answers SHALL NOT supply corrections or examples. Expected facts for a fresh check SHALL come from an independent source-key writer before the answer actor runs.

#### Scenario: A development repair receives a fresh check

- **WHEN** the repair is ready for a fresh verification case
- **THEN** the key writer derives its expected facts from permitted source truth before generation, separately from the answer actor

#### Scenario: Held-out material is offered for iteration

- **WHEN** a learning candidate includes frozen held-out material
- **THEN** that material is excluded from the learning workflow without restating it in a prompt or fixture

### Requirement: Closure states the behavior actually verified

The workflow SHALL distinguish committed data, successful retrieval, and useful downstream answers. It SHALL retain failed and missing checks and SHALL NOT close a miss from a write receipt alone. A claimed improvement SHALL identify its cases, versions, instrument, failures, and case-level comparison with uncertainty.

#### Scenario: A write succeeds but the answer still fails

- **WHEN** the correction commits and a later answer misses the supported fact
- **THEN** the incident retains the successful write and failed answer, and useful-answer verification remains open

#### Scenario: A difference is compatible with zero

- **WHEN** a measured before/after interval includes zero
- **THEN** the workflow reports no change rather than an improvement

### Requirement: Learning stays with the active agent

The workflow SHALL add no server-side semantic classifier, generative model call, or autonomous data writer. Background learning and model backfill SHALL remain default-off. Missing evidence or unavailable optional checks SHALL leave the incident unresolved without blocking ordinary recall.

#### Scenario: Optional verification cannot run

- **WHEN** a check lacks capacity or permitted evidence
- **THEN** the workflow records the unresolved check and ordinary recall remains available
