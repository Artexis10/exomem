## ADDED Requirements

### Requirement: Every Bootstrap Profile Carries Minimum Authoring Semantics

The `compact`, `full`, and `diagnostics` bootstrap profiles SHALL each include the
same versioned minimum semantic-authoring object. It SHALL contain the exact
compact syntax, canonical `## Observations` section, open category versus governed
kind distinction, one-valid-unit minimum for new active compiled notes, compact
versus rich choice and rich heading boundary, preferred typed write routes, Tier-2 applicability,
stable refusal codes, and remediation. Expanded profiles MAY add examples but
SHALL NOT weaken or contradict the minimum object.

#### Scenario: Default bootstrap is enough to write correctly
- **WHEN** a generic client calls `bootstrap()` with the default compact profile
- **THEN** the response alone explains how to author a valid active compiled note, default to compact `[category]` form, and choose a non-empty rich unit without duplicating content

#### Scenario: Profiles agree on normative fields
- **WHEN** compact, full, and diagnostics bootstrap responses are compared
- **THEN** their semantic-authoring contract version and normalized normative fields are identical

#### Scenario: Bootstrap remains content-blind
- **WHEN** bootstrap returns semantic-authoring guidance for a populated vault
- **THEN** the guidance is built without reading note bodies and contains no vault-derived example, path, project key, or identifier
