## MODIFIED Requirements

### Requirement: Planning represents intended future state

The Planning profile SHALL represent goals, desired outcomes, ongoing areas, initiatives, priorities, commitments, horizons, and candidate future work. It SHALL NOT represent observed events or measurements, raw received material, proof artifacts, compiled conclusions, imported staging, or companion-owned execution artifacts. Planning SHALL NOT infer success, failure, health, priority, completion, or personal judgment from elapsed time, Records, pointers, or external systems.

#### Scenario: Encountered bug becomes candidate work
- **WHEN** a user asks Exomem to retain an encountered bug for possible future work
- **THEN** Planning can capture it as a candidate work item without turning it into a Record, compiled Note, or accepted companion artifact

#### Scenario: Observed event remains a Record
- **WHEN** a user reports that a training session, transaction, symptom, measurement, or maintenance event happened
- **THEN** the event remains Records observed state rather than becoming Planning intent

#### Scenario: Review and interpretation remain explicit
- **WHEN** a plan links to recorded observations
- **THEN** Planning preserves only the authored intent and evidence pointer and does not infer progress or compile a conclusion

### Requirement: Capture and triage remain deliberate

Planning SHALL support rapid candidate capture and an explicit triage transition without automatic classification. Triage SHALL operate only on an active-lifecycle outcome, initiative, or work item, require a non-empty exact `transition` mapping, and MAY change only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`. Kind changes SHALL remain among those three deliverable kinds; `area` and `parent` MAY be explicit null to clear them. Triage SHALL revalidate the complete resulting item and hierarchy with the current container hash and item version and SHALL NOT infer values from title/body text. Areas, lifecycle, health, dates, tags, evidence, execution, domain fields, title, and complete body replacement SHALL remain add/update-only.

#### Scenario: Feature request enters inbox cheaply
- **WHEN** a user asks to retain a feature request without choosing priority or schedule
- **THEN** Planning stores it in the default candidate inbox state and does not ask for irrelevant storage details

#### Scenario: Triage promotes intent without copying execution contracts
- **WHEN** a candidate that already carries an opaque companion pointer becomes a committed quarterly initiative
- **THEN** one guarded triage mutation updates only the Planning transition fields while companion-owned requirements and tasks remain absent from the item

#### Scenario: Stale triage refuses
- **WHEN** a direct edit changes the item after the agent read it
- **THEN** triage refuses the prior item version and preserves the human edit

### Requirement: Thin external execution pointers

A Planning item MAY carry at most 16 `execution` mappings. Each mapping SHALL contain exactly `kind` and `ref`, plus optional `label`. `kind` SHALL be 1–64 ASCII bytes matching the open workflow key syntax `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`; existing values `openspec`, `repository`, `issue`, `pull-request`, `release`, `deployment`, and `other` SHALL remain valid but none SHALL be privileged. `ref` SHALL be non-empty opaque text of at most 2,048 UTF-8 bytes; `label` SHALL be non-empty text of at most 256 UTF-8 bytes. No phase, health, task state, requirements, tests, code, remote response, or other machine-readable payload is permitted inside the pointer. Planning SHALL NOT fetch, resolve, mirror, target-authorize, or treat the pointer kind as proof of a companion capability. The Planning item's top-level health is the only coarse authored health projection; when a workflow contract declares an external owner, detailed execution truth remains with that declared owner.

#### Scenario: Companion work stays visible but thin
- **WHEN** a Planning initiative is accepted into an artifact whose type the resolved workflow contract assigns to a companion
- **THEN** the item retains planning identity, hierarchy, horizon, top-level authored health, and the opaque companion pointer without copying artifact contents or remote phase

#### Scenario: Promoted software work stays visible but thin
- **WHEN** a Planning initiative is accepted into an OpenSpec change
- **THEN** the item retains planning identity, hierarchy, horizon, top-level authored health, and the OpenSpec pointer without copying the change artifacts or remote phase

#### Scenario: Existing OpenSpec pointer remains valid data
- **WHEN** an existing item carries `kind: openspec`
- **THEN** validation continues to accept the pointer, but its kind alone neither selects a workflow contract nor makes OpenSpec the product-wide execution authority

#### Scenario: New companion key needs no validator release
- **WHEN** an item carries a syntactically valid kind for a companion Exomem does not know
- **THEN** Planning validates and round-trips it as opaque data without requiring a tool-specific enum or adapter

#### Scenario: External state does not mutate Planning automatically
- **WHEN** a linked pull request merges or deployment changes outside Exomem
- **THEN** Planning remains unchanged until an explicit edit or contract-permitted proposal followed by user approval

#### Scenario: Opaque reference cannot disclose a hidden vault target
- **WHEN** an execution reference resembles a local path, stable ID, or inaccessible target
- **THEN** Planning treats it as bounded opaque text and does not resolve it into content or public ambiguity

## ADDED Requirements

### Requirement: Planning works standalone or under a resolved companion boundary

Planning SHALL be fully functional with the built-in standalone workflow decision. When a valid companion workflow contract resolves, Planning SHALL continue to own durable intent, desired outcomes, priorities, commitments, horizons, areas, and hierarchy while declared companion-owned artifacts remain external. Planning MAY store opaque execution references and concise connective context but SHALL NOT require, resolve, mirror, or synchronize companion artifacts.

#### Scenario: Standalone software planning needs no second system
- **WHEN** standalone resolves for a software project
- **THEN** outcomes, initiatives, and work items remain usable in Exomem Planning without requiring OpenSpec, an issue tracker, or another integration

#### Scenario: Companion requirements stay with the companion
- **WHEN** a contract declares that a companion owns `software.requirements` and `software.acceptance-tasks`
- **THEN** Planning retains the durable goal and opaque artifact reference without copying the requirements or task list into canonical Planning fields

### Requirement: Durable stated intent routes to existing Planning without a magic verb

When the active workflow contract and prominence policy permit proactive durable-intent capture, an agent SHALL inspect the relevant Planning inventory when the user commits to, sequences, reprioritizes, starts, or scopes durable future work. It SHALL update one unambiguous matching item when possible and SHALL NOT create a parallel prose note or duplicate Planning item merely because the user did not say “save”, “Planning”, or “Exomem”. Tentative exploration SHALL remain uncaptured unless the user explicitly asks.

#### Scenario: Starting scoped work activates the existing item
- **WHEN** a user says to begin work that unambiguously matches an existing candidate Planning item under a proactive contract
- **THEN** the agent uses guarded triage/update on that item and does not create a duplicate item or generic note

#### Scenario: Mid-flight idea remains conversational
- **WHEN** a user explores a possible direction without commitment under the proactive posture
- **THEN** the agent does not turn the idea into committed Planning state

#### Scenario: Explicit-only contract waits for an ask
- **WHEN** the applicable contract sets durable-intent capture to explicit
- **THEN** ordinary discussion creates no Planning mutation until the user asks to retain or change the intent
