# context-activation (delta)

## ADDED Requirements

### Requirement: Served units carry their own authored time
Every unit in the packet SHALL carry, as `updated`, the ISO date its author wrote in
the unit's context slot (the compact `(context)` or the rich `context:` row). A date
the context introduces as a deadline (due, by, before, until, deadline, expiry) SHALL
NOT count, and a date mentioned only in the unit's prose SHALL NOT count. A unit that
authors no time SHALL be served with an empty `updated`. Within a role, units SHALL
order by their own time and then by their page's time, which is internal and SHALL NOT
be published.

#### Scenario: Old observation on a recently edited page
- **WHEN** a page updated on 2026-09-20 holds a unit whose context reads `decision on 2026-03-04`
- **THEN** the served unit's `updated` is 2026-03-04

#### Scenario: A deadline is not the unit's time
- **WHEN** a unit reads "renew the certificate before it expires on 2027-01-15", or its context reads `due 2027-01-15`
- **THEN** its `updated` is empty

### Requirement: History comes only from an authored supersession
A unit SHALL carry `history: true` only when its lifecycle is `superseded` or
`archived`: its page is superseded or archived, or a unit on the SAME page carries a
`supersedes` relation naming it (by `#anchor`, `page#anchor` or a full-path link),
which gives it lifecycle `superseded`. No date, category or shared-word comparison
SHALL mark a unit history, and draft, planned or dropped material SHALL NOT be
history. Within a role, history SHALL rank after current material; role order is
unchanged. A relation on another page SHALL mark nothing, so a released unit never
reports that a page its reader may not see replaced it.

#### Scenario: An authored relation retires its sibling
- **WHEN** a unit carries `relations: supersedes: [[relay-trial#cand-a]]` and `cand-a` is on the same page
- **THEN** `cand-a` is served with lifecycle `superseded` and `history: true`, after the superseding unit

#### Scenario: A newer decision does not retire an unrelated fact
- **WHEN** a page holds a fact dated 2026-08-01 and an unrelated decision dated 2026-09-01
- **THEN** the fact is served current

#### Scenario: An outcome retires a recommendation only by an authored link
- **WHEN** an outcome shares words with an earlier recommendation but carries no `supersedes` relation to it
- **THEN** the recommendation stays current

### Requirement: Weakly resolved anchors do not vouch for current state
When an anchor's only evidence is shared words or recall (lexical overlap, rare term,
recency, category match, usage prior, retrieval), its `current_state` SHALL be held
back when older than 30 days, SHALL carry `resolved_by: lexical` and `age_days` when
dated within 30 days, and SHALL be served with `resolved_by: lexical` when undated. An
anchor with any stronger evidence SHALL be unchanged.

#### Scenario: Old state on a lexically resolved anchor
- **WHEN** an anchor resolves on `lexical_overlap` alone and its state is 90 days old
- **THEN** no `current_state` entry is served for it

#### Scenario: Undated state on a lexically resolved anchor
- **WHEN** an anchor resolves on `rare_term` alone and its state is undated
- **THEN** the entry is served with `resolved_by: lexical`

### Requirement: A resolved entity or hub serves the current-state page it declares
When an entity or hub anchor resolves and its own page names a neighbourhood page in
the `current_state_page` frontmatter field, `current_state[]` SHALL carry that page's
leading `fact` or `config` unit with `source: canonical_page`, the page's `path`, and
the unit's own authored time as `as_of`; an undated unit SHALL instead carry the page's
time as a `page updated <date>` statement label, leaving `as_of` empty. The
internal `page_updated` field SHALL NOT be published. An ambiguous bare page name
SHALL NOT select any page. No other page SHALL be treated as canonical. The egress guard
SHALL decide the entry's `path` and drop the entry when that page is withheld. The
entry SHALL be charged to the packet budget.

#### Scenario: The declared page is served
- **WHEN** an entity page declares `current_state_page: "[[harbour-operating-state]]"` and that page opens with "Retainer agreement is signed"
- **THEN** `current_state[]` carries that sentence with `source: canonical_page` and the page's path

#### Scenario: No declaration, no canonical state
- **WHEN** the anchor's page declares no current-state page and a newer neighbourhood page carries facts
- **THEN** no `canonical_page` entry is served

#### Scenario: A restricted declared page stays withheld
- **WHEN** the declared page is withheld from the caller's audience
- **THEN** no text of it appears in the packet

### Requirement: Currency inputs are not published
The packet SHALL NOT publish internal `supersession`, `supersedes_targets` or
`page_updated` fields. Undated current state may label its page's time in its
statement; that label SHALL count against the statement and packet budgets. A
successor's name SHALL reach the packet only through a field the egress guard
decides.

#### Scenario: A withheld successor is named in a relation
- **WHEN** a released unit's `supersedes` relation names a withheld page in any spelling
- **THEN** no spelling of that page's name appears in an external-scope packet

### Requirement: No unit class is exempt from the role cap
Every unit, including standing units and units that carry a supersession relation,
SHALL count against `MAX_ITEMS_PER_ROLE`.

#### Scenario: Many supersession units
- **WHEN** more units carrying `supersedes` relations than `MAX_ITEMS_PER_ROLE` qualify for one role
- **THEN** the excess become `role_cap` pointers

### Requirement: The hook marks history
The prompt hook SHALL render a history unit with the label `history` (or `carried
history` for a carried one) in place of `unit` (or `carried`).

#### Scenario: A superseded unit in the rendered block
- **WHEN** the packet serves a unit with `history: true`
- **THEN** its rendered line begins `- history: `
