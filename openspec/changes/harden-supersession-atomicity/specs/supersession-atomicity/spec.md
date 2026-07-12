## ADDED Requirements

### Requirement: Atomic Supersession

A supersession SHALL either fully commit its bidirectional chain — the new page, the old page's `status: superseded` + `superseded_by` flip, and the log entry — or make no change at all. A failure partway through MUST NOT leave a standalone new page with a dangling `supersedes` pointer or a half-updated chain.

#### Scenario: A failure mid-supersession leaves no dangling page

- **WHEN** a supersession fails after the new-page content is prepared but before the old-page chain flip commits
- **THEN** no standalone new page remains and the old page is unchanged (the operation is all-or-nothing)

### Requirement: Single-Winner Concurrent Supersession

Concurrent supersessions of the same active page SHALL NOT both succeed. The system MUST detect that the old page changed between the eligibility read and the write (a compare-and-swap on its content version) and refuse the losing call with a stale error, so exactly one successor chain results.

#### Scenario: Two concurrent replaces produce exactly one successor

- **WHEN** two `replace` calls target the same active old page concurrently and both pass the initial "not already superseded" check
- **THEN** exactly one commits a successor chain
- **AND** the other is refused with a stale/changed error and writes nothing

#### Scenario: Re-superseding an already-superseded page is still refused

- **WHEN** a `replace` targets a page that is already `status: superseded`
- **THEN** it is refused and no second successor is created
