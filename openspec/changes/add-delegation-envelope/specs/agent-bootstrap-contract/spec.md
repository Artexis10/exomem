## ADDED Requirements

### Requirement: The agent contract teaches the delegation envelope compactly

Bootstrap SHALL serve the active envelope — each v1 action class with its
ceiling and current disposition, marked derived or overridden — and SHALL teach
the decider protocol: name the class before acting; treat an above-ceiling
intent as a proposal, never an act; honour the disposition (`off` — do not do
it; `advisory` — surface in domain language and stop; `silent` — act, with
narration governed by prominence; `confirm` — ask before executing); and record
outcomes through triage. The teaching SHALL name the standing-delegation
refusal so the agent does not improvise one. Across every carrier — compact
bootstrap, scaffold and plugin skill copies, and the hookless
custom-instructions block — the envelope teaching SHALL fit a measured budget
of at most fifty lines in total, and compact bootstrap SHALL stay within its
existing byte ceiling, with before and after sizes recorded when the
implementation lands.

#### Scenario: The contract names every class and the protocol

- **WHEN** compact bootstrap is served at any prominence level
- **THEN** the engagement guidance names all six v1 action classes with their
  ceilings and current dispositions and states the four-step decider protocol

#### Scenario: A hookless client receives the same teaching

- **WHEN** the hookless custom-instructions block is generated
- **THEN** it carries the envelope teaching within the shared fifty-line
  budget, and its instructions match the served envelope rather than
  restating a hardcoded table
