## MODIFIED Requirements

### Requirement: Outcome suites measure task outcome over sessions

The benchmark SHALL run three outcome suites as registered operational families: CL-Bench `database_exploration` for outcome over sessions, Harvey LAB for multi-document synthesis, and a planted wrong-note suite for governance. CL-Bench and LAB SHALL run from upstream commits pinned in one lock file, and their questions, documents, answer keys and criteria SHALL come from those commits. The planted suite's cases and keys SHALL come from an independent case-authoring lane and SHALL stay in evaluator custody outside the repository. Each suite SHALL compare arms that differ only in the memory treatment, as its amendment entry states. The primary Exomem arm SHALL be the product as a user gets it: the shipped plugin with its skills and hooks at the default prominence, plus the published custom-instruction block for that level, read from its document at run time, and no instruction written for the benchmark. The one exception is a per-task memory sentence that a benchmark gives every memory system: the primary Exomem arm SHALL receive that sentence too, pointed at Exomem, and the same arm without it SHALL be reported as the product gap, description only. Bare-MCP and other reduced Exomem arms SHALL be labelled ablations and SHALL NOT decide.

#### Scenario: The bare-MCP arm outscores the shipped arm

- **WHEN** the bare-MCP Exomem arm scores higher than the shipped arm
- **THEN** the decision still reads the primary Exomem arm against the controls
- **AND** the report shows the ablation difference as description

#### Scenario: The benchmark tells every memory system to store what it learns

- **WHEN** the folder arm receives the benchmark's per-task memory sentence
- **THEN** the primary Exomem arm receives the same sentence pointed at Exomem
- **AND** the same arm without the sentence is reported as the product gap, description only

#### Scenario: A pinned upstream checkout is at another commit

- **WHEN** the run root holds a CL-Bench or LAB checkout at a commit other than the lock file's pin
- **THEN** the suite refuses to run
- **AND** the refusal names the checkout, its commit and the pinned commit

### Requirement: Outcome actors and judges are isolated

Outcome actors SHALL run headless on the subscription, with project-only settings, a strict MCP configuration, an out-of-tree working directory, auto memory off and path-scoped file permissions. Their environment SHALL NOT reach the live personal vault, the live service state or the user's MCP configuration, and each arm's Exomem state SHALL live inside its arm directory. A judge SHALL see an opaque item id, the task, the key or criteria and the answer, and SHALL NOT see the arm, the vault or the recall packet. The judgement file SHALL record the judge's model id, and every judged report SHALL name whether the judge shares the actor's model family. When the decision judge shares it, a judge from another family SHALL re-judge a sample once it has room, and the report SHALL give the agreement as description. A held-out report judged by a model other than the frozen decision judge, or a held-out run whose actor model or effort differs from the frozen one, SHALL NOT read as decision evidence.

#### Scenario: The parent shell points at the live vault

- **WHEN** the harness starts an actor from a shell whose environment names the live vault and service state
- **THEN** the actor's environment carries none of those values
- **AND** its Exomem state resolves inside the arm directory

#### Scenario: The decision judge is from the actor's family

- **WHEN** an f36 or f37 decision report is judged by a Claude model while the actor is a Claude model
- **THEN** the report names the judge family as the same as the actor's
- **AND** a later cross-family sample re-judge adds its agreement without changing the decision
