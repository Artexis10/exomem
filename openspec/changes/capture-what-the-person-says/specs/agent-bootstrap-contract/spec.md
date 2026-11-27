## MODIFIED Requirements

### Requirement: The capture predicate saves each claim at its true strength

At a stepping stone, the capture predicate SHALL include four classes besides the durable conclusion, the recurring entity and the executed method. The agent SHALL save each class at its true strength: the agent SHALL NOT write a claim more certain, more general or more settled than its source made it.

- a held view, which is a view someone holds, such as an interpretation or a working hypothesis that the evidence does not settle, saved attributed to its holder with its uncertainty and never as an established fact; a momentary reaction is not a held view;
- a person's unsure report that later work could use, saved as unsure and attributed to that person, with its doubt, and never as a fact, an event or a baseline;
- a question the work leaves open, saved as an open question, including whether something happened at all;
- the concrete values a task settled, such as times, prices, terms, contacts and limits, saved in full and never as a pointer to a working file.

Trivia, one-offs and momentary reactions get no note, because nothing later needs them. A remark with no reuse value, and mid-thought exploration or a tangent, SHALL remain unwritten. The reason is reuse value, not uncertainty. A tentative claim SHALL NOT be written as an event that happened.

The activation `capture_policy` block, at every disposition except `off`, SHALL carry these classes and the rule, as `context-activation` states. So SHALL each of these scaffold surfaces: `_Schema/SKILL.md`, `references/engagement.md`, `workflow-skills/exomem-capture/SKILL.md` and `workflow-skills/exomem-reflect/SKILL.md`. The bootstrap core SHALL NOT state that tentative or unresolved material in general stays unwritten. A copyable custom-instruction block SHALL NOT state that tentative or unresolved material in general stays unwritten. A held view, an unsure report, an open question or settled values SHALL count as a stepping stone even when nothing was decided. A write SHALL NOT mention material that a capture rule leaves unwritten, not even to say that it was left out.

#### Scenario: A held view is saved attributed, with its uncertainty

- **WHEN** a task ends with a view that the evidence does not settle, held by the user or by the agent
- **THEN** the agent saves it at the stepping stone, attributed to its holder
- **AND** the saved text states its uncertainty and does not present it as an established fact

#### Scenario: A question the work leaves open is saved as an open question

- **WHEN** a task ends with a question that the work raised and did not answer
- **THEN** the agent saves the question as an open question
- **AND** it does not drop the question because it is unresolved

#### Scenario: Values set in a working file are saved as the values themselves

- **WHEN** a task settles concrete values, such as a time, a price, a term or a contact, and records them in a working file
- **THEN** the saved material states each value in full
- **AND** it does not save only a pointer to the working file

#### Scenario: A casual tentative personal claim stays quiet

- **WHEN** a person says in passing that they might switch gyms, and nothing later needs it
- **THEN** the agent saves no held view and performs no durable write for that statement
- **AND** mid-thought exploration also stays unwritten

#### Scenario: A person's unsure report is saved as unsure

- **WHEN** a person says they think something happened but are not sure, and later work could use the report
- **THEN** the agent saves the report attributed to that person, and states the doubt in the saved text
- **AND** it does not save the report as a fact, as an event or as a baseline

#### Scenario: A claim is saved no stronger than its source

- **WHEN** a source says that one client asked for a feature, and a person guesses that the total is about forty
- **THEN** the saved text names one client and does not say that clients ask for the feature
- **AND** the saved text gives the total as a guess that it attributes to the person, not as a count

#### Scenario: A question the work leaves open about an event is saved

- **WHEN** the work leaves open whether something happened at all
- **THEN** the agent saves that as an open question
- **AND** it does not write the event as having happened

#### Scenario: A session that ends on a held view saves it

- **WHEN** a session ends with a view that someone holds and nothing was decided
- **THEN** the agent saves the view at that point, attributed to its holder, with its uncertainty

#### Scenario: Unwritten material leaves no trace in a write

- **WHEN** a trivial one-off or a mid-thought tangent stays unwritten
- **THEN** no write mentions it
- **AND** no write records that it was left out

## ADDED Requirements

### Requirement: The capture predicate covers what the person says about themselves, whatever the task

When the person tells the agent something lasting about their aims, priorities, the people they know, their belongings or their past, which a later session would need, the agent SHALL save it in that turn, whatever the task, at its true strength and under `proactive_capture`. A named relationship SHALL go on that person's Entity when one resolves uniquely. Incidental associations and momentary reactions SHALL stay unwritten, as `durable-personal-baseline-capture` states.

#### Scenario: A durable statement in passing is saved whatever the task

- **WHEN** a session's task is operational, such as coordinating other sessions or watching CI, and the person mentions in passing that a colleague they know built a tool with a partner, and that they think they started their current project in the spring
- **THEN** under `silent` the agent saves both statements in that turn, the second attributed to the person with its hedge, and under `advisory` it offers the save in one line
- **AND** it saves the relationship on the colleague's Entity when one resolves uniquely, without being asked
- **AND** the agent judges what counts; no word list or detector decides it

#### Scenario: A momentary reaction about a person stays unwritten

- **WHEN** the person says during an operational task that a colleague's new dashboard looks great today
- **THEN** the agent performs no durable write for that reaction

### Requirement: The native MCP activation nudge states the capture rule for what the person says

At `balanced` and `maximal`, the native MCP activation nudge SHALL carry one sentence that states this capture rule itself, not a pointer to a rule a recall served, because after a compaction the agent may no longer hold that rule. At `light`, which writes only on request, the nudge SHALL NOT carry it. The served capture `rules` SHALL stay within the hook's 1,000-character `rules` bound.

#### Scenario: The rule reaches an agent that no longer holds the recalled rule

- **WHEN** at `balanced` a session was compacted after its last `recall_context`, and the person then states a lasting fact about themselves during an operational task
- **THEN** the activation nudge for that turn carries the capture sentence
- **AND** the sentence names the classes and the true-strength condition without referring to an earlier recall

#### Scenario: The light level leaves the sentence out

- **WHEN** the hook's level is `light` and the native MCP activation nudge is sent
- **THEN** the nudge does not carry the capture sentence
