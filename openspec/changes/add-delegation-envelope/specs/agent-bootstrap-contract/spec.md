## ADDED Requirements

### Requirement: The agent contract teaches the delegation envelope compactly

Bootstrap SHALL serve the active envelope: the five disposition-bearing v1
action classes each with ceiling, current disposition, and
fixed/derived/overridden provenance, and the `disclosure` class marked
governance-owned with no disposition. It SHALL teach the decider protocol —
name the class before acting; treat an above-ceiling intent as a proposal,
never an act; honour the disposition (`off`: do not initiate, though an
explicit user request is never blocked; `advisory`: surface in domain language
and stop; `silent`: act, narration governed by prominence; `confirm` /
`confirm-shortcut`: obtain the confirmation first); record outcomes through
triage — and SHALL name the founder-gate refusal for `restructure_execution`
so the agent does not improvise one. The same teaching SHALL land on every
carrier (compact bootstrap, scaffold and plugin skill copies, the hookless
custom-instructions block) within a measured budget of at most fifty lines per
carrier, with compact bootstrap additionally within its existing byte ceiling
and before/after sizes recorded when the implementation lands.

#### Scenario: The contract names every class and the protocol

- **WHEN** compact bootstrap is served at any prominence level
- **THEN** the engagement guidance names all six v1 action classes — five with
  ceiling, disposition, and provenance, and `disclosure` as governance-owned —
  and states the decider protocol including the founder-gate refusal

#### Scenario: A hookless client receives the same teaching

- **WHEN** the hookless custom-instructions block is generated
- **THEN** it carries the envelope teaching within its per-carrier fifty-line
  budget, and its instructions defer to the served envelope rather than
  restating a hardcoded table
