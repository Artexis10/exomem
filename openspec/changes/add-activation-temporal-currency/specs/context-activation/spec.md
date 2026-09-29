# context-activation (delta)

## ADDED Requirements

### Requirement: Served units carry their own time
Every unit in the packet SHALL carry the time the unit itself authors (an ISO date in
its context or text), never the time of its parent page. A unit that authors no time
SHALL be served with an empty `updated`, and the parent page's time SHALL appear only
as `provenance.page_updated`.

#### Scenario: Old observation on a recently edited page
- **WHEN** a page updated on 2026-09-20 holds a unit dated 2026-01-10
- **THEN** the served unit's `updated` is 2026-01-10 and not 2026-09-20

#### Scenario: Undated unit
- **WHEN** a unit authors no date
- **THEN** its `updated` is empty and `provenance.page_updated` names the page's time

### Requirement: History is labelled and ranked below current material
A unit that is superseded, that an explicit supersession relation replaces, or that is
older than a dated current decision on the same page SHALL carry `history: true` and
SHALL rank below every current unit regardless of role. A unit carrying a `supersedes`
relation SHALL be served in full and SHALL NOT be reduced to a `role_cap` pointer.

#### Scenario: Superseded candidate beside a current decision
- **WHEN** a page holds a decision dated 2026-03-04 and an observation dated 2026-01-10
- **THEN** the observation is marked history and follows the decision

#### Scenario: Supersession units exceed the role cap
- **WHEN** more supersession units than `MAX_ITEMS_PER_ROLE` qualify
- **THEN** all are served and none becomes a `role_cap` pointer

### Requirement: Weakly resolved anchors do not vouch for current state
When an anchor's only evidence is shared words (lexical overlap, rare term, recency,
category match or usage prior), its `current_state` SHALL be held back when older than
30 days or undated, and SHALL carry `resolved_by: lexical` and `age_days` otherwise.
An anchor with any stronger evidence SHALL be unchanged.

#### Scenario: Old state on a lexically resolved anchor
- **WHEN** an anchor resolves on `lexical_overlap` alone and its state is 90 days old
- **THEN** no `current_state` entry is served for it

### Requirement: A later outcome supersedes an earlier recommendation
When one page holds a recommendation unit and a later-dated outcome unit sharing at
least three content terms with it, the outcome SHALL be served with `newer_than`
naming the recommendation, and the recommendation SHALL carry `history: true` and
`superseded_by_outcome` naming the outcome.

#### Scenario: Recommended action already done
- **WHEN** a recommendation dated 2026-08-01 and an outcome dated 2026-08-15 record the same action
- **THEN** the outcome ranks first and the recommendation is marked superseded by it

### Requirement: A resolved project or entity serves its canonical current-state page
When a project, entity or hub anchor resolves and its neighbourhood holds a canonical
current-state page (the page the anchor's own page names in `current_state_page`, else
the newest page carrying `fact` or `config` units), `current_state[]` SHALL carry that
page's leading unit with `source: canonical_page` and the unit's OWN observed time as
`as_of`. The entry SHALL be charged to the packet budget and, being budgeted before
units, SHALL take precedence over history units. The anchor-resolution weakness rule
above applies to it unchanged.

#### Scenario: Settled relationship is served, not left uncertain
- **WHEN** a turn names a recurring project whose newest state page opens with "Executed
  consultancy agreement between Northwind Ltd and Tidewater Co"
- **THEN** `current_state[]` carries that sentence with `source: canonical_page` and the
  unit's own date, ahead of older history units

#### Scenario: The hub names the current page
- **WHEN** the anchor's page names a page in `current_state_page` and a newer page also
  carries state units
- **THEN** the named page's leading unit is served
