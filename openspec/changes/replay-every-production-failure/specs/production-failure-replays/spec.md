## ADDED Requirements

### Requirement: One permanent set holds every production-failure replay

The incident runner's case files SHALL be the only home of a real-model replay case for a production failure. A case SHALL NOT be removed or held out. A case that no longer reproduces SHALL stay in the set and keep running. A held-out case used while iterating on a fix SHALL move into the set and never move back.

#### Scenario: A new production failure gets one case

- **WHEN** an agent misbehaves with Evermatter in real use and the failure is replayed
- **THEN** its case is added to the incident runner's case files, and no other suite or external folder holds a second copy

#### Scenario: A held-out case used while iterating joins the set

- **WHEN** a held-out case is run on a candidate and a base arm while a fix is still changing
- **THEN** the case is rewritten, admitted and added to the set, and is no longer used as held-out

### Requirement: Every case has a committed admission line

A committed admission record SHALL hold one line per admitted case: its id and digest, its kind (positive, guard or legacy), the arms' commits, the client class, the model, the repeats and the per-arm passed, failed and unknown counts. The runner SHALL refuse a case whose digest has no admission line, except in an admission run, which writes the line.

#### Scenario: A changed case needs a new admission

- **WHEN** a case's turns, seed or checks change after it was admitted
- **THEN** its digest changes, and the runner refuses it until a new admission line names the new digest

#### Scenario: A fix PR shows its replays

- **WHEN** a PR fixes a production failure that has a case
- **THEN** the PR's diff adds the admission line that records the case's counts on the arm without the fix and on the fix arm

### Requirement: Positive cases reproduce and guard cases name what they protect

A positive case SHALL be admitted only when it fails on at least two of three repeats on an arm without the fix. A fix PR's line SHALL show the targeted checks passing on at least two of three repeats on the fix arm. A guard case SHALL name the positive case that it protects, and SHALL be admitted when it passes on at least two of three repeats on both arms. An existing case SHALL get a legacy line that names the commit that added it.

#### Scenario: A rewrite that does not reproduce is refused

- **WHEN** a new positive case passes on the arm without the fix
- **THEN** the case is not admitted, and its author rewrites it or records why the failure cannot be replayed

#### Scenario: A guard against over-recall is kept

- **WHEN** a guard case checks that a general question gets no memory lookup and passes on both arms
- **THEN** it is admitted beside the positive case that it names

### Requirement: Cases are fresh rewrites that keep only the mechanism

Each case file SHALL carry an `incident` line with the failure's date and its mechanism in neutral words. A lane that never saw the original turns SHALL write each case from the mechanism alone. A case SHALL NOT paraphrase a real turn or keep a real person's life facts under new names.

#### Scenario: Private details stay out

- **WHEN** a case is written from a real incident
- **THEN** its people, places, numbers and life facts are invented, its turns are not paraphrases of the real ones, and the link to the private incident record lives only in the operator's vault

### Requirement: The runner plays the client class a failure happened on

The incident runner SHALL offer the two skills-only client classes: the uploaded core skill, and the plugin's skills without hooks. In both, the server serves no instructions over either transport, the tools are deferred behind a tool search, and the served surface is claude.ai. A case SHALL name its client class.

#### Scenario: A claude.ai incident replays on its own client class

- **WHEN** a case from a claude.ai conversation runs on the uploaded-skill client class over HTTP
- **THEN** the agent sees the core skill and a deferred tool list, no server instructions, and the claude.ai surface

### Requirement: Checks read what was surfaced, when, and what changed

A case SHALL be able to require that a named page is surfaced by a read before a named turn's answer or before a write; that a named turn makes no read whose result surfaces a page or row of the vault; that named pages change, stay unchanged or are created; and that a named turn calls a named tool or door operation.

#### Scenario: A recall that misses the page fails

- **WHEN** a case requires a named page before turn 3's answer and the agent's only read on turn 3 returns other pages
- **THEN** the check fails, although a read happened

#### Scenario: A lookup on an earlier turn does not count

- **WHEN** a case requires a read on turn 3 and the agent reads only on turn 1
- **THEN** the turn 3 check fails

### Requirement: Tool classes, refusals and answers are read without word lists

A check SHALL read whether a call reads or writes from the arm: the `readOnlyHint` annotation of the tool, or the effect of the door operation that the call names, and whether a call was refused from the error code in its result. Answer content SHALL be judged by the blind judge against the case's reference. No check that this change adds or ports SHALL match tool names from a hand-kept list, or refusal or answer text against words.

#### Scenario: A recall counts as a read

- **WHEN** a case requires a read on a turn and the agent calls only `recall_context` on that turn
- **THEN** the check counts the call as a read, and its result is searched for the named page

#### Scenario: A renamed read tool still counts

- **WHEN** an arm renames a read tool and a case requires a read on a turn
- **THEN** a call to the renamed tool satisfies the check

#### Scenario: A refused call is read from its code

- **WHEN** a call returns an error code for an unexpected argument
- **THEN** the run records the call as refused, whatever words the result's message uses

### Requirement: A case can seed a whole vault

A case SHALL be able to name a seed directory in the case folder as well as inline seed notes. The case digest SHALL cover its seed directory.

#### Scenario: Editing a seed changes the digest

- **WHEN** one file of a case's seed directory changes
- **THEN** the case digest changes and the case needs a new admission line
