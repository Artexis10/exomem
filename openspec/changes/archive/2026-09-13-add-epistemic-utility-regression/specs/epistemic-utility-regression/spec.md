## ADDED Requirements

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
