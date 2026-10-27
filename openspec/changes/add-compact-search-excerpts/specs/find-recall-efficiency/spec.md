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

When a release strips a source from a hit, the hit SHALL carry no `excerpt`, in compact and in full mode,
whether or not a length cut shortened the excerpt. This covers page, unit and matched-unit excerpts.

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

- **WHEN** the release plane withholds a page in full mode
- **THEN** the compact hit for the same page is withheld
- **AND** no text appears in a compact excerpt that the whitespace-collapsed full-mode excerpt does not carry

#### Scenario: A hit under a release strip carries no excerpt

- **WHEN** a release strips a source from a hit, whether the excerpt was cut by length or is the whole text of its page
- **THEN** the hit carries no `excerpt`, in compact and in full mode
- **AND** the hit keeps its path, title and ref

#### Scenario: Full mode remains the default

- **WHEN** `find` is called without a detail mode
- **THEN** each returned hit has the existing full shape, including `excerpt` and any existing
  optional fields that would have been present before this change
- **AND** the ranking and default return type are unchanged

#### Scenario: Compact mode preserves ranking and routing metadata

- **WHEN** the same `find` request is made once with full detail and once with compact detail
- **THEN** the ordered paths are identical
- **AND** compact hits still include the metadata needed to choose a follow-up `get` call
