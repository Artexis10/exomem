## ADDED Requirements

### Requirement: Unfinished Experiment Lifecycle Review Queue

The audit registry SHALL expose an `unfinished_experiments` category that surfaces an
experiment page whose declared window has closed without a recorded result. A page
SHALL be surfaced when all of the following hold: its `type` is `experiment`, its
`started` frontmatter parses to a calendar date, its `duration` frontmatter parses to a
finite span of whole days, the elapsed days from `started` to today exceed that span,
and no non-empty `outcome:` is recorded. A page whose `duration` is open-ended or
otherwise does not parse to a finite span MUST NOT be surfaced, because an experiment
that declares no deadline cannot have missed one.

The queue SHALL exclude pages whose `status` is `archived`, `superseded`, or `draft`,
index and log pages, and pages outside a read-write access tier, matching the scope
discipline of the existing measurement-only queues. Findings SHALL carry severity
`info` and SHALL be ordered oldest-first by elapsed days since `started`, with the
vault-relative path as a deterministic tiebreak. Each finding SHALL carry a stable
`meta.signal_version` derived from the page's authored state, plus `started`,
`duration_days`, `elapsed_days`, and `overdue_days`.

The check SHALL be a pure measurement over recorded frontmatter. It MUST NOT write,
conclude, archive, or infer an outcome, and it MUST NOT change `find` ordering. Its
proposed fix SHALL defer the decision to the reader among recording the result,
extending the duration, and archiving the experiment.

#### Scenario: An experiment past its window with no outcome is surfaced

- **WHEN** an active experiment declares `started` 120 days ago and `duration` of
  `30 days` and records no `outcome:`
- **THEN** an `unfinished_experiments` finding is emitted for that page at `info`
  severity whose meta reports 120 elapsed days and 90 overdue days
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A recorded outcome closes the loop

- **WHEN** the same experiment records a non-empty `outcome:`
- **THEN** no `unfinished_experiments` finding is emitted for it

#### Scenario: A concluded status without an outcome still surfaces

- **WHEN** an experiment past its window carries `status: concluded` but records no
  `outcome:`
- **THEN** an `unfinished_experiments` finding is still emitted, because the status
  records that the experiment stopped and not what it showed

#### Scenario: An open-ended duration is never overdue

- **WHEN** an experiment declares `duration: ongoing` and a `started` date years in the
  past with no `outcome:`
- **THEN** no `unfinished_experiments` finding is emitted

#### Scenario: An experiment still inside its window is not surfaced

- **WHEN** an active experiment declares `started` 10 days ago and `duration` of
  `30 days`
- **THEN** no `unfinished_experiments` finding is emitted

#### Scenario: The queue is ordered oldest-first

- **WHEN** several experiments are past their windows with different elapsed ages
- **THEN** the findings are emitted with the largest elapsed age first and ties broken
  by vault-relative path

### Requirement: Unfinished Experiments Is Registered But Not Default In Attention

`unfinished_experiments` SHALL be a valid `attention` category, selectable through the
existing `categories` filter, and SHALL be rejected by neither the attention validator
nor the audit validator. It MUST NOT be a member of the default attention category
union, so an `attention` call made without a category filter SHALL NOT surface an
`unfinished_experiments` item.

The exclusion SHALL be justified by backlog profile rather than by category kind. The
`started` and `duration` fields this check reads long predate it, so an established
vault can already hold a large population of long-closed windows, and admitting them to
the daily surface at upgrade time would displace the signal already there. A sibling
epistemic-lifecycle category whose fields are new enough to have no such population
SHALL NOT be excluded on this reasoning.

Selecting a category set that omits `unfinished_experiments` SHALL reproduce the prior
behaviour of both `audit` and `attention` exactly, so the category can be disabled
without residue.

#### Scenario: The default attention surface excludes the category

- **WHEN** `attention` is called without a category filter over a vault holding an
  overdue experiment
- **THEN** no `unfinished_experiments` item is surfaced

#### Scenario: The category is selectable

- **WHEN** `attention` is called with `categories=["unfinished_experiments"]`
- **THEN** the overdue experiment is surfaced as a ranked review item carrying its
  reason, and no `ValueError` is raised for the category name

#### Scenario: The category reaches the audit surface

- **WHEN** `audit` is called with `categories=["unfinished_experiments"]`
- **THEN** the overdue experiment is reported and the summary counts it under that
  category name
