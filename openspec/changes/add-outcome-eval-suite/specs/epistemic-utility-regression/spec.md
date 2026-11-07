## ADDED Requirements

### Requirement: Outcome suites measure task outcome over sessions

The benchmark SHALL run three outcome suites as registered operational families: CL-Bench `database_exploration` for outcome over sessions, Harvey LAB for multi-document synthesis, and a planted wrong-note suite for governance. CL-Bench and LAB SHALL run from upstream commits pinned in one lock file, and their questions, documents, answer keys and criteria SHALL come from those commits. The planted suite's cases and keys SHALL come from an independent case-authoring lane and SHALL stay in evaluator custody outside the repository. Each suite SHALL compare arms that differ only in the memory treatment, as its amendment entry states. The primary Exomem arm SHALL be the product as a user gets it: the shipped plugin with its skills and hooks at the default prominence, plus the published custom-instruction block for that level, read from its document at run time, and no instruction written for the benchmark. Bare-MCP and other reduced Exomem arms SHALL be labelled ablations and SHALL NOT decide.

#### Scenario: The bare-MCP arm outscores the shipped arm

- **WHEN** the bare-MCP Exomem arm scores higher than the shipped arm
- **THEN** the decision still reads the shipped arm against the controls
- **AND** the report shows the ablation difference as description

#### Scenario: A pinned upstream checkout is at another commit

- **WHEN** the run root holds a CL-Bench or LAB checkout at a commit other than the lock file's pin
- **THEN** the suite refuses to run
- **AND** the refusal names the checkout, its commit and the pinned commit

### Requirement: Outcome evaluation has one command

Outcome evaluation SHALL expose one command group: `membench outcome fetch` prepares the pinned upstreams, `membench outcome run <suite>` runs a suite end to end, `membench outcome report <suite>` judges where the suite needs a judge and writes per-case results with paired intervals, and `membench outcome panel` records release-panel evidence. A run SHALL default to the development split. All run data SHALL live under one run root outside the repository, set by `EXOMEM_OUTCOME_ROOT`.

#### Scenario: An operator runs a development round

- **WHEN** an operator runs `membench outcome run planted --run-id r2`
- **THEN** the suite builds both vaults and answers every development case
- **AND** `membench outcome report planted --run-id r2` writes the blind judgement and the paired table

### Requirement: Held-out outcome runs are gated by release and frozen inputs

A held-out run SHALL call the family release gate before its first session, so a pending amendment refuses it. Before its first session it SHALL also check every held-out input against the amendment receipt's `fixture_sha256`, with paths relative to the run root. A missing or changed file SHALL refuse the run. A development run SHALL NOT call the release gate.

#### Scenario: The amendment is not yet acknowledged

- **WHEN** an operator starts a held-out run of a family whose receipt is pending
- **THEN** the run refuses before any session with the typed acknowledgment-pending error
- **AND** the refusal names the family and the pending amendment sequence

#### Scenario: A held-out case file changed after freezing

- **WHEN** a held-out input's SHA-256 differs from the receipt's digest
- **THEN** the run refuses before any session
- **AND** the refusal names the file and both digests

### Requirement: Outcome results are labelled internal diagnostics

Every outcome report SHALL carry the label "internal diagnostic", because the actors run on a subscription and not under the suites' official protocols. A development or panel report SHALL state that it is instrument evidence. Only a held-out report under an acknowledged amendment SHALL state that it is decision evidence. No outcome figure SHALL be presented as a published suite result.

#### Scenario: A development round shows a large gain

- **WHEN** a development report shows a paired difference whose interval excludes zero
- **THEN** the report still reads as instrument evidence
- **AND** it supports no comparative claim

### Requirement: Outcome actors and judges are isolated

Outcome actors SHALL run headless on the subscription, with project-only settings, a strict MCP configuration, an out-of-tree working directory, auto memory off and path-scoped file permissions. Their environment SHALL NOT reach the live personal vault, the live service state or the user's MCP configuration, and each arm's Exomem state SHALL live inside its arm directory. A judge SHALL see an opaque item id, the task, the key or criteria and the answer, and SHALL NOT see the arm, the vault or the recall packet. A decision judge SHALL come from another model family than the actor, and the judgement file SHALL record its model id. A held-out report judged by another model, or a held-out run whose actor model or effort differs from the frozen one, SHALL NOT read as decision evidence.

#### Scenario: The parent shell points at the live vault

- **WHEN** the harness starts an actor from a shell whose environment names the live vault and service state
- **THEN** the actor's environment carries none of those values
- **AND** its Exomem state resolves inside the arm directory

### Requirement: Each release records an outcome panel

After a release, the panel command SHALL run from a clean checkout of that release's tag, SHALL run the Exomem arms on a fixed panel drawn only from the development cases, and SHALL write per-case results to `docs/benchmarks/outcome/<version>.json` and `.md`. The panel SHALL compare each case, paired, against the previous version's stored results when they exist. The panel is evidence and SHALL NOT gate a release. The control arms SHALL run again only when the actor model or CLI version changes.

#### Scenario: The model changed since the last panel

- **WHEN** the actor's reported model differs from the previous panel's
- **THEN** the panel reruns the control arms before comparing
- **AND** the report names the model change
