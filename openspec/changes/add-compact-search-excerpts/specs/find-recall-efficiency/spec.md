## MODIFIED Requirements

### Requirement: Compact and Full Find Result Surfaces

The system SHALL support a `find` result detail mode with `full` and `compact` values. `full` SHALL
be the default and SHALL preserve the current hit dictionary shape. `compact` SHALL return the same
ranked hits in a token-cheap shape that includes routing fields such as path, title, type, scope,
updated date, lifecycle status, media pointers, out-of-KB marker, and clip timestamp when present,
and MUST omit detailed ranking signals unless a future explicit option asks for them.

A compact page hit SHALL also carry `excerpt` when the hit has one, and SHALL carry no `excerpt` field
when it has none. The compact excerpt SHALL be cut to at most 240 characters at a word boundary,
followed by a trailing ellipsis marker when it was cut. It SHALL pass through the same release projection as the
full-mode excerpt, and the cut SHALL follow every release strip, so a compact excerpt, without its marker, is a
prefix of the whitespace-collapsed excerpt that full mode releases for the same hit.

#### Scenario: Compact mode omits token-heavy fields

- **WHEN** `find` is called with compact detail mode
- **THEN** each returned hit includes its path and title
- **AND** each returned hit that has an excerpt includes `excerpt`, within the compact limit
- **AND** each returned hit omits detailed `signals`

#### Scenario: A long compact excerpt is cut at a word boundary

- **WHEN** a page hit has an excerpt longer than the compact limit
- **THEN** the compact excerpt ends at a word boundary within 240 characters and carries a trailing ellipsis marker
- **AND** the full-mode excerpt of the same hit is unchanged

#### Scenario: A hit without an excerpt has no compact excerpt field

- **WHEN** a page hit has an empty excerpt
- **THEN** its compact form has no `excerpt` field

#### Scenario: A withheld excerpt stays withheld in compact mode

- **WHEN** the release plane withholds a page, or strips an identity from its excerpt, in full mode
- **THEN** the compact hit for the same page is withheld, or carries the same stripped text cut to the compact limit
- **AND** no text appears in a compact excerpt that the whitespace-collapsed full-mode excerpt does not carry

#### Scenario: An excerpt cut inside a link to a stripped source leaves no fragment

- **WHEN** a release strips a source from a hit, and the length cut of a page, unit or matched-unit excerpt starts or ends inside a link or path to that source
- **THEN** the excerpt, in compact and in full mode, carries no part of that link or path
- **AND** an excerpt that is complete or names no stripped source is unchanged

#### Scenario: Full mode remains the default

- **WHEN** `find` is called without a detail mode
- **THEN** each returned hit has the existing full shape, including `excerpt` and any existing
  optional fields that would have been present before this change
- **AND** the ranking and default return type are unchanged

#### Scenario: Compact mode preserves ranking and routing metadata

- **WHEN** the same `find` request is made once with full detail and once with compact detail
- **THEN** the ordered paths are identical
- **AND** compact hits still include the metadata needed to choose a follow-up `get` call
