## MODIFIED Requirements

### Requirement: Outcome suites measure task outcome over sessions

The benchmark SHALL run four outcome suites as registered operational families: CL-Bench `database_exploration` for outcome over sessions, Harvey LAB for multi-document synthesis, a planted wrong-note suite for governance, and an epistemic task suite for the whole memory loop. CL-Bench and LAB SHALL run from upstream commits pinned in one lock file, and their questions, documents, answer keys and criteria SHALL come from those commits. The planted and epistemic suites' cases and keys SHALL come from an independent case-authoring lane and SHALL stay in evaluator custody outside the repository. Each suite SHALL compare arms that differ only in the memory treatment, as its amendment entry states. The primary Exomem arm SHALL be the product as a user gets it: the shipped plugin with its skills and hooks at the default prominence, plus the published custom-instruction block for that level, read from its document at run time, and no instruction written for the benchmark. The one exception is a per-task memory sentence that a benchmark gives every memory system: the primary Exomem arm SHALL receive that sentence too, pointed at Exomem, and the same arm without it SHALL be reported as the product gap, description only. Bare-MCP and other reduced Exomem arms SHALL be labelled ablations and SHALL NOT decide.

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

## ADDED Requirements

### Requirement: The epistemic task suite measures the whole memory loop
Family f38 SHALL measure task outcome when the actor builds its own memory over a project's scripted work sessions while the project's knowledge changes.
- The workspace SHALL reset before every session to that session's scripted files, identically in every arm, so that nothing from an earlier session survives except through the arm's memory.
- A probe's task and files SHALL NOT contain its answer or its trap. A premise probe is the one exception: its task SHALL contain the stale or false state that it presupposes. The key SHALL hold that state as its premise, apart from its traps, and the rule above SHALL still cover the key's value, its accepted phrasings and its traps.
- Every change probe SHALL depend on one planted change and SHALL name one epistemic dimension key. Stable probes SHALL ask for facts that never change, so the report separates recall from currency.
- A probe SHALL run from a copy of the arm's memory as the session's main task left it, and the runner SHALL discard the probe's memory writes.
- The arms N, F, P and E SHALL differ only in the memory treatment. Arm F SHALL receive the product's learning instruction pointed at its folder, so the comparison is between stores.
- The runner SHALL advance all arms one session at a time. A session that still fails from an infrastructure fault after its declared retries SHALL invalidate that project's window for every arm. A product failure SHALL stay scored.
- The blind judge SHALL decide each probe's correctness from the task, the key and the answer. A deterministic match SHALL be reported only as a cross-check, and only on a structured answer field.

#### Scenario: A reversed decision is used as it stands now
- **WHEN** session 4 records that staging deploys need one approval and session 7 reverses it to two
- **AND** the probe in session 10 asks the actor to prepare a staging deploy, with files that state neither number
- **THEN** the judge marks the probe correct only when the actor requires two approvals
- **AND** the report files the probe under the `currency` dimension

#### Scenario: A correct answer in other words counts
- **WHEN** the key holds "two approvals" and the answer says "a second reviewer must sign off"
- **THEN** the judge marks the answer correct and the decision statistic counts it as correct

#### Scenario: Arms cannot drift apart in time
- **WHEN** a run plays session 5
- **THEN** no arm starts session 6 before every arm has finished session 5

#### Scenario: A probe that holds its own answer is refused
- **WHEN** a probe's task or its session's files contain the probe's key value, an accepted phrasing or its trap
- **THEN** the case check refuses the case set before any run and names the probe

#### Scenario: A provider error does not count as a loss
- **WHEN** arm F's session 6 fails with a provider error after its retries
- **THEN** that project's window is invalid for every arm, and the report lists it with the error
- **AND** no probe in that window counts as a loss for any arm

### Requirement: The epistemic task suite gates its probes in development
Before any held-out run, a development round on the dev project SHALL check every probe twice.
- **Solvable:** arm N, given the full text of every earlier session's task and files in its prompt, SHALL solve at least 90% of probes. Each probe it fails SHALL be repaired or dropped.
- **Memory needed:** arm N without that text SHALL fail at least 80% of probes. Each probe it passes SHALL be repaired or dropped. A probe whose key names its reading as harm is the one exception: an abstention probe, or a task that no stored memory bears on. Arm N is expected to pass such a probe. This gate SHALL skip it, and the report SHALL give it as harm against arm N.
The report SHALL list both rates and every probe they removed.

#### Scenario: A probe the bare actor can guess is removed
- **WHEN** arm N without history answers a probe correctly in the dev round
- **AND** the probe's key does not name its reading as harm
- **THEN** the probe is repaired or dropped before the held-out split is frozen
