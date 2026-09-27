## ADDED Requirements

### Requirement: Unavailable vocabulary guidance names a closed reason

When post-commit vocabulary guidance is unavailable, the committed response's
`vocabulary_sync` SHALL name the closed projection reason that caused it, such as
`graph_projection_unavailable`, `target_projection_unavailable`, or
`target_projection_changed`, and SHALL fall back to `guidance_unavailable` only when the cause
is outside that closed set. A reason outside the closed set SHALL NOT be released. An
exception swallowed by optional guidance or its recovery SHALL be logged at warning level
naming the exception type and raising code location, and SHALL NOT log the exception message
or page content. The committed outcome, receipt identity and replay behaviour SHALL be
unchanged.

#### Scenario: An unpublished graph snapshot is named

- **WHEN** content commits while the epistemic graph snapshot is unavailable
- **THEN** `vocabulary_sync` is `unavailable` with reason `graph_projection_unavailable`
- **AND** the recovery route to vocabulary review is still present

#### Scenario: A swallowed failure is visible to the operator

- **WHEN** optional guidance raises after commit
- **THEN** the response reports `guidance_unavailable`
- **AND** one warning log line names the exception type without its message

### Requirement: A tag variant advisory uses the single advisory slot

A committed write whose page carries a tag that is a fold-variant of a more-used tag SHALL be
able to carry one `vocabulary_advisory` of family `tag-variant/v1` naming the authored tag and
the canonical tag in a single line, with a route to tag-variant maintenance. It SHALL be
emitted only when the structural-suggestion disposition is not `off`, SHALL be validated
against its closed shape before public projection, and SHALL NOT displace a relation
vocabulary review notice. Computing it SHALL NOT fail or alter the committed outcome.

#### Scenario: A balanced write names the canonical tag

- **WHEN** a write at a non-`off`, non-`maximal` level authors `dogfooding` while `dogfood` is used on more pages
- **THEN** the page keeps `dogfooding`
- **AND** the response carries one `tag-variant/v1` advisory naming `dogfood`

#### Scenario: Review work keeps the slot

- **WHEN** the same write also produces a relation vocabulary review notice
- **THEN** the review notice is the advisory and the tag notice is omitted
