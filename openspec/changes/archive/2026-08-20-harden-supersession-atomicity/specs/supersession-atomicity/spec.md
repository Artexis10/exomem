## ADDED Requirements

### Requirement: Atomic Supersession

A supersession SHALL either fully commit its complete planned write set — the new page, source backreferences, navigation indexes, the old page's `status: superseded` + `superseded_by` flip, and the combined note/replace log update — or make no change at all. A failure partway through MUST NOT leave a standalone new page with a dangling `supersedes` pointer, a half-updated chain, or note side effects from a supersession that did not commit.

#### Scenario: A failure during destination replacement rolls back the full write set

- **WHEN** an injected failure occurs after at least one destination in the supersession batch is replaced but before the complete batch commits
- **THEN** no standalone new page remains
- **AND** the old page retains its pre-supersession content and active status
- **AND** source backreferences, navigation indexes, and the log retain their pre-supersession content

### Requirement: Single-Winner Concurrent Supersession

Concurrent supersessions of the same active page SHALL NOT both succeed. The system MUST detect that the old page changed between the eligibility read and the write (a compare-and-swap on its content version) and refuse the losing call with a stale error, so exactly one successor chain results.

#### Scenario: Two concurrent replaces produce exactly one successor

- **WHEN** two `replace` calls target the same active old page concurrently and both perform their eligibility read against the same content version
- **THEN** exactly one commits a successor chain
- **AND** the committed old page points to that winner without dropping its pointer
- **AND** the other is refused with `STALE_SUPERSEDE` and writes no new page, backreference, index, or log side effect

#### Scenario: Re-superseding an already-superseded page is still refused

- **WHEN** a `replace` targets a page that is already `status: superseded`
- **THEN** it is refused with `ALREADY_SUPERSEDED`
- **AND** no second successor or note side effect is created

#### Scenario: The old page changes after the eligibility read

- **WHEN** the old page's content hash changes after `replace` reads it as eligible but before the supersession batch commits
- **THEN** the supersession is refused with `STALE_SUPERSEDE`
- **AND** none of its planned writes are committed
