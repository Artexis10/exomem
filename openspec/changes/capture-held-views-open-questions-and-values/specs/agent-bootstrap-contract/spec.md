## ADDED Requirements

### Requirement: The capture predicate covers held views, open questions and established values

At a stepping stone, the capture predicate SHALL include three classes besides the durable conclusion, the recurring entity and the executed method:

- a held view, which is a view someone holds, such as an interpretation or a working hypothesis, saved attributed to its holder with its uncertainty and never as an established fact;
- a question the work leaves open, saved as an open question;
- the concrete values a task settled, such as times, prices, terms, contacts and limits, saved in full and never as a pointer to a working file.

The activation `capture_policy` block, at every disposition except `off`, SHALL carry the three classes. So SHALL each of these scaffold surfaces: `_Schema/SKILL.md`, `references/engagement.md`, `workflow-skills/exomem-capture/SKILL.md` and `workflow-skills/exomem-reflect/SKILL.md`. The bootstrap core SHALL NOT state that tentative or unresolved material in general stays unwritten. A copyable custom-instruction block SHALL NOT state that tentative or unresolved material in general stays unwritten. The rule that tentative claims stay quiet SHALL be scoped to personal baseline context wherever it is served. A casual tentative personal claim, and mid-thought exploration or a tangent, SHALL remain unwritten. A person's unsure report of an event or its details is a tentative event, not a question left open, and SHALL remain unwritten unless a collection offers a note field for it. A question the work leaves open, including whether something happened, SHALL still be saved as an open question. A held view, an open question or settled values SHALL count as a stepping stone even when nothing was decided. A write SHALL NOT mention material a capture rule keeps unwritten, not even to say that it was left out.

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

- **WHEN** a person says in passing that they might switch gyms
- **THEN** the agent performs no durable write for that statement
- **AND** mid-thought exploration also stays unwritten

#### Scenario: A person's unsure report of an event stays unwritten

- **WHEN** a person says they think something happened but are not sure, and no collection offers a note field for it
- **THEN** the agent writes nothing about it
- **AND** it does not save the doubt as an open question

#### Scenario: A question the work leaves open about an event is saved

- **WHEN** the work leaves open whether something happened
- **THEN** the agent saves it as an open question

#### Scenario: A session that ends on a held view saves it

- **WHEN** a session ends with a view that someone holds and nothing was decided
- **THEN** the agent saves the view at that point, attributed to its holder, with its uncertainty

#### Scenario: Unwritten material leaves no trace in a write

- **WHEN** a person's unsure report of an event or a mid-thought tangent stays unwritten
- **THEN** no write mentions it
- **AND** no write records that it was left out
