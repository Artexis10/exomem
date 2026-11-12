# epistemic-utility-regression Specification

## Purpose
Measure whether Exomem improves or harms downstream agent actions through cheap, paired seeded episodes, with explicit lifecycle controls, adverse outcomes, reproducible identities and cost limits.

## Requirements

### Requirement: Downstream utility is measured through paired action outcomes

The benchmark SHALL compare the same configured actor with no Exomem and with a pinned Exomem revision on matched seeded episodes. Success SHALL be determined from resulting environment state through deterministic assertions, not retrieval recall, tool-call counts or agent claims. It SHALL report utility lift and paired outcome counts per scenario family without a weighted aggregate.

#### Scenario: Correct recall precedes a wrong action

- **WHEN** the memory arm retrieves the relevant fact but applies an invalid configuration
- **THEN** its downstream outcome is a failure
- **AND** the successful recall remains a separate diagnostic

### Requirement: Lifecycle and context experiments have distinct claims

Whole-system utility episodes SHALL include agent-authored capture, a maintenance opportunity and a fresh-session action through shipped product surfaces. Attentiveness and context aggressiveness SHALL be separate manifest fields. If a later context-policy experiment is supported, it SHALL hold the witnessed memory checkpoint fixed and SHALL NOT claim to measure capture or maintenance utility; this first slice does not require implementing that optional control.

#### Scenario: JIT uses a shared checkpoint

- **WHEN** two context policies consume the same agent-authored checkpoint
- **THEN** their result is labelled a context-mechanism comparison
- **AND** creation costs and checkpoint identity remain available separately

### Requirement: Controls isolate the memory treatment

Arms SHALL share actor settings, common tools, starting task world, ordinary workspace persistence, a matched notice of that persistence, clocks and declared resource/retry limits. The no-memory arm SHALL have no Exomem access. The treatment SHALL be labelled product-as-shipped, including its instruction cost. Actor views SHALL exclude the evaluator manifest, seeds, target hashes, evaluator targets, future evidence and personal operator state. If a later oracle control is supported, its context SHALL contain sufficient time-valid facts without supplying the expected action.

#### Scenario: Private target can be read from a workspace

- **WHEN** a guard finds an evaluator target reachable through actor files, metadata or tools
- **THEN** the run is rejected as an instrument fault before comparative execution

### Requirement: Adverse outcomes remain in the denominator

The protocol SHALL freeze its failure taxonomy before comparative runs. Product failures, zero retrieval, missed capture, wrong actions and declared task-budget exhaustion SHALL remain scored outcomes where the environment is evaluable. Only evidenced infrastructure faults SHALL invalidate an attempted pair. Scheduled, attempted, valid, invalid and missing pairs SHALL be reported, with all incurred or held costs retained.

#### Scenario: Memory consumes its action budget

- **WHEN** the control succeeds and the memory arm exhausts its declared task budget without completing the action
- **THEN** the pair records a memory loss and contributes to harm
- **AND** the memory arm is not removed as an incomplete session

### Requirement: Harm and efficiency are explicit

Reports SHALL show per-family harm among successful controls with its denominator, unconditional paired losses, per-arm prohibited/destructive-action counts by frozen class, and matched per-family oracle gap and denominators when available. They SHALL show phase-level token and monetary cost, wall time, model/tool time, turns and context volume per family. A zero harm denominator SHALL be undefined. Usage accounting SHALL avoid double-counting nested reasoning tokens. Diagnostic integrity failures SHALL remain visible without suppressing a valid downstream loss.

#### Scenario: Every control fails

- **WHEN** no valid control succeeds
- **THEN** conditional harm is reported as undefined with denominator zero
- **AND** both-fail counts and coverage remain visible

### Requirement: Paired episodes bind reproducible identities

Paired episodes SHALL bind scenario hashes, seed, product revision, actor requested/reported identity, provider, effort, tool/skill/config versions and budgets. Incompatible arms SHALL not support paired claims. Model or provider fallback SHALL be disabled. A small pilot SHALL be described as instrument evidence, not general performance evidence. Candidate-versus-reference product revision monitoring is deferred to a follow-up change.

#### Scenario: An arm's model identity changes

- **WHEN** the memory arm reports a model or provider incompatible with its control manifest
- **THEN** a paired utility claim is refused
- **AND** the attempted run and its cost are retained

### Requirement: Paid probes are bounded and opt-in

Ordinary instrument tests SHALL run without model calls or network access. Paid probes SHALL require explicit execution selection, known pricing, per-phase limits and a total budget. The existing ledger SHALL reserve a conservative complete-pair amount before launch and retain unknown charges. Failure SHALL produce explicit outcome data without automatic frontier fallback or an expensive replay restart.

#### Scenario: Remaining budget cannot cover the next pair

- **WHEN** the conservative reservation for the next pair exceeds the remaining cap
- **THEN** the pair is not started
- **AND** the report identifies unattempted coverage without imputing success

### Requirement: Existing benchmark governance and infrastructure are reused

The implementation SHALL extend `membench` and reuse seeded/oracle, isolation, witness and ledger foundations. It SHALL resolve the shared actor integration with PR #1126 and use a thin adapter rather than duplicate its loop or require a generic runtime refactor. New scoring and denominator semantics SHALL be registered as an operational family in the existing epistemic registry and released by its versioned amendment receipt before comparative execution or claims. Synthetic actors and scripted compilation SHALL establish instrument properties only.

#### Scenario: A reference actor exposes poisoned-memory harm

- **WHEN** a deterministic reference actor fails under poisoned context during an instrument test
- **THEN** the result establishes grader sensitivity
- **AND** it is not reported as measured Exomem agent harm

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

Every outcome report SHALL carry the label "internal diagnostic", because the actors run on a subscription login or on API credits and not under the suites' official protocols. A development or panel report SHALL state that it is instrument evidence. Only a held-out report under an acknowledged amendment SHALL state that it is decision evidence. No outcome figure SHALL be presented as a published suite result.

#### Scenario: A development round shows a large gain

- **WHEN** a development report shows a paired difference whose interval excludes zero
- **THEN** the report still reads as instrument evidence
- **AND** it supports no comparative claim

### Requirement: Outcome actors and judges are isolated

Outcome actors SHALL run headless on the billing source that their run declares, either the Claude subscription login or Claude API credits, with project-only settings, a strict MCP configuration, an out-of-tree working directory, auto memory off and path-scoped file permissions. Their environment SHALL NOT reach the live personal vault, the live service state or the user's MCP configuration, and each arm's Exomem state SHALL live inside its arm directory. Every actor session and every Claude judge call SHALL record the credential source it reports, and a session that reports a source other than the declared one SHALL be a harness fault. The billing source SHALL NOT change the evidence class of a held-out report once the amendment that allows both sources is acknowledged; until then a held-out report billed to credits SHALL read as instrument evidence. A judge SHALL see an opaque item id, the task, the key or criteria and the answer, and SHALL NOT see the arm, the vault or the recall packet. The judgement file SHALL record the judge's model id, and every judged report SHALL name whether the judge shares the actor's model family. When the decision judge shares it, a judge from another family SHALL re-judge a sample once it has room, and the report SHALL give the agreement as description. A held-out report judged by a model other than the frozen decision judge, or a held-out run whose actor model or effort differs from the frozen one, SHALL NOT read as decision evidence.

#### Scenario: The parent shell points at the live vault

- **WHEN** the harness starts an actor from a shell whose environment names the live vault and service state
- **THEN** the actor's environment carries none of those values
- **AND** its Exomem state resolves inside the arm directory

#### Scenario: The decision judge is from the actor's family

- **WHEN** an f36 or f37 decision report is judged by a Claude model while the actor is a Claude model
- **THEN** the report names the judge family as the same as the actor's
- **AND** a later cross-family sample re-judge adds its agreement without changing the decision

#### Scenario: A held-out run is billed to API credits

- **WHEN** a gated held-out run declares credits, every session reports credits, and its actor and judge are the frozen ones
- **THEN** the report reads as decision evidence once the billing amendment is acknowledged
- **AND** it reads as instrument evidence while that amendment is pending

#### Scenario: A session reports another credential source than the run declared

- **WHEN** a run declares the subscription and an actor session reports an API key as its credential source
- **THEN** the session is a harness fault under the suite's fault rules
- **AND** the run file records the declared and the observed source

### Requirement: Each release records an outcome panel

After a release, the panel command SHALL run from a clean checkout of that release's tag, SHALL run the Exomem arms on a fixed panel drawn only from the development cases, and SHALL write per-case results to `docs/benchmarks/outcome/<version>.json` and `.md`. The panel SHALL compare each case, paired, against the previous version's stored results when they exist. The panel is evidence and SHALL NOT gate a release. The control arms SHALL run again only when the actor model or CLI version changes.

#### Scenario: The model changed since the last panel

- **WHEN** the actor's reported model differs from the previous panel's
- **THEN** the panel reruns the control arms before comparing
- **AND** the report names the model change
