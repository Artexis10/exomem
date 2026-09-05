## ADDED Requirements

### Requirement: The audit detects recurring unresolved entity identities

The audit SHALL provide an `entity_recurrence` category that counts, per
NFKC-normalised identity, the distinct pages whose bodies carry an unresolved
wikilink to it, and SHALL produce one finding per identity that reaches the
spread gate while resolving against neither an existing vault page nor the
entity registry's titles and aliases. The finding SHALL carry reason
`unresolved_identity_recurs`, the candidate identity, the sorted mentioning
pages, and a bounded deterministic list of registry near-matches. The category
SHALL add no write-time work, no embedding, no model call, and SHALL never
create any page.

#### Scenario: A recurring unresolved identity becomes a candidate

- **GIVEN** three distinct pages whose bodies link an identity that exists neither as a page nor as a registry title or alias
- **WHEN** the audit sweeps the category
- **THEN** one `entity_recurrence` finding is produced carrying reason `unresolved_identity_recurs`, the candidate, the three pages sorted, and the near-match list

#### Scenario: Frequency inside one page is not spread

- **GIVEN** one page linking the same unresolved identity five times and one other page linking it once
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity

#### Scenario: Plain-text mentions are out of scope for this stream

- **GIVEN** an identity mentioned in body text of many pages with no wikilink anywhere
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for it

#### Scenario: A registry-resolved identity is never a candidate

- **GIVEN** an entity page whose alias NFKC-matches an identity linked from five pages
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity

#### Scenario: Retired and excluded pages are not evidence

- **GIVEN** an identity linked from two eligible pages and from one page that is superseded, archived, draft, or in an excluded access tier
- **WHEN** the audit sweeps the category
- **THEN** zero findings are produced for that identity
- **AND** a finding never anchors on a retired or excluded page

#### Scenario: Acting on the advice resolves it by state change

- **GIVEN** a firing candidate
- **WHEN** an entity page whose title or alias resolves the identity is created, or the linked target page itself is created
- **THEN** the finding is not produced on the next sweep
- **AND** deleting that page brings the finding back without any recorded dismissal

#### Scenario: Findings ride the existing review machinery

- **GIVEN** an `entity_recurrence` finding
- **WHEN** it is delivered
- **THEN** it is a fingerprint-bound review item keyed on the identity, honouring family dispositions and dismissal suppression
