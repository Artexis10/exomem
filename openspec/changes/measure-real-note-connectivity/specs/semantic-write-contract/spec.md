## MODIFIED Requirements

### Requirement: Current Relation Review Disposition

Every newly created active governed compiled page SHALL have a current relation-review
disposition satisfied by a qualifying typed inbound/outbound relation, a qualifying
outbound connectivity signal, an explicit reviewed-none decision bound to current page
identity/content, or an automatic no-candidates bootstrap disposition only for a genuinely
empty governed corpus.

A relation qualifies as a **typed edge** only when it is authored or explicitly
reviewer-accepted, its target resolves unambiguously to an eligible governed page at
evaluation time, its canonical registry entry is active and scope-valid, its registry
family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or
`provenance`, and either (a) its origin is `markdown_relation`, `semantic_relation`, or
`semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly
`supersession`. The sole inactive-target exception is a canonical supersession edge to the
exact governed predecessor now marked `superseded` whose `superseded_by` resolves back to
the active successor.

When no typed edge qualifies, the disposition MAY be satisfied by an **outbound
connectivity signal**: an authored outbound connection whose target resolves unambiguously
to a connectable governed page, whose canonical registry entry is active and scope-valid,
and whose origin is `wikilink`, `markdown_relation`, `semantic_relation`, or
`semantic_block`, irrespective of registry family. The connectable set SHALL be the
eligible governed set widened to admit append-only `Sources/` material, and SHALL be
computed separately from the eligible governed set so that the empty-corpus bootstrap
disposition is unaffected by captured sources.

Frontmatter origin SHALL NOT satisfy connectivity. This keeps the disposition aligned with
the relation-debt measurement, which clears a page on authored relation rows or body
wikilinks and ignores `sources:`. Provenance is a vertical edge to raw material and carries
no claim about how a conclusion relates to other conclusions; because every
adoption-compiled note cites a source by construction, counting it would make the
disposition a no-op for exactly the bulk-import case that most needs review.

Inbound edges MUST NOT satisfy connectivity. Unresolved or ambiguous forward targets MUST
NOT satisfy either lane. A disposition satisfied by connectivity SHALL report that signal
distinctly from a typed edge and SHALL emit a non-blocking typed-edge-absent warning. The
disposition SHALL carry any reviewed-none reason and reference it was satisfied by, so
review decisions are observable rather than write-only. No minimum relation count SHALL be
imposed in either lane.

#### Scenario: Superseded predecessor remains a qualifying target
- **WHEN** an active successor canonically supersedes a governed predecessor and the predecessor is marked `superseded` with an exact backlink to that successor
- **THEN** the supersession edge remains qualifying after commit and during exact prepared recovery
- **AND** no other inactive target receives this exception

#### Scenario: Typed relation satisfies disposition
- **WHEN** a new compiled page has a registered canonical relation to an existing page
- **THEN** its relation disposition is satisfied and reports the typed edge
- **AND** no typed-edge-absent warning is emitted

#### Scenario: Reviewed none is fingerprint-bound
- **WHEN** a reviewer records that a page has no qualifying relation candidates
- **THEN** the disposition is satisfied only while the page identity and relevant content fingerprint remain current
- **AND** a material change resurfaces relation review
- **AND** the recorded reason is reported on the disposition rather than discarded

#### Scenario: Empty vault bootstraps without fake edge
- **WHEN** the first compiled page is created in a genuinely empty governed corpus and no target can exist
- **THEN** the writer records an automatic bootstrap disposition without fabricating a relation or placeholder

#### Scenario: Captured source does not consume the bootstrap exception
- **WHEN** a vault contains only captured `Sources/` material and the first compiled page is created
- **THEN** the page still receives the automatic bootstrap disposition
- **AND** the connectable target set does not widen the eligible governed set used to detect an empty corpus

#### Scenario: Bootstrap exception expires when a candidate can exist
- **WHEN** a second eligible compiled page is added after the first page received an automatic bootstrap disposition
- **THEN** the first page's bootstrap disposition becomes stale and enters ordinary relation review

#### Scenario: Excluded-family relation satisfies connectivity without a typed edge
- **WHEN** an active compiled page carries no qualifying typed relation but one or more authored relation rows of an excluded family resolving to connectable governed pages
- **THEN** its relation disposition is satisfied and reports the connectivity signal rather than a typed edge
- **AND** a typed-edge-absent warning is emitted at warning severity
- **AND** the write is not blocked

#### Scenario: Body wikilinks are measured but not yet a disposition signal
- **WHEN** an active compiled page carries only body wikilinks and no authored relation row
- **THEN** the links are recorded on page state for measurement
- **AND** the disposition remains unsatisfied, because emitting them as relation facts regressed the semantic write-latency gate and the fact emission was withdrawn pending a cheaper resolution path

#### Scenario: Cited provenance alone does not satisfy the disposition
- **WHEN** an active compiled page carries no typed relation and no body wikilink, but names existing `Sources/` pages in its `sources:` frontmatter
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** an adoption-compiled page, which always cites a source, still receives relation review

#### Scenario: A body wikilink to a Source does satisfy connectivity
- **WHEN** an active compiled page links a `Sources/` page from its body rather than only from frontmatter
- **THEN** the disposition is satisfied by the connectivity signal
- **AND** the target is not required to be an eligible governed page

#### Scenario: Inbound links and provenance back-references never satisfy connectivity
- **WHEN** a compiled page has no outbound connection of any kind, but a `Sources/` page links to it and its own `ingested_into:` back-reference names it
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** automatically written back-references cannot make the disposition vacuous

#### Scenario: Excluded-family relation does not qualify as a typed edge but may connect
- **WHEN** a page's only relation is in the `citation`, `evidence`, or `link` family and resolves to a connectable governed page
- **THEN** it does not qualify as a typed edge
- **AND** it satisfies the disposition as connectivity, reported distinctly, with a typed-edge-absent warning

#### Scenario: Inactive relation does not qualify in either lane
- **WHEN** a page has only a relation whose canonical registry entry is inactive or out of scope
- **THEN** the edge remains visible in its own semantics and the disposition remains unsatisfied

#### Scenario: Unresolved forward target does not qualify
- **WHEN** a registered authored relation points to a missing or ambiguous target
- **THEN** the relation remains visible with its resolution finding but does not satisfy relation review
- **AND** it does not satisfy connectivity

#### Scenario: Reviewed-none is not applicable to a connected page
- **WHEN** a caller submits a reviewed-none decision for a page that satisfies the disposition through connectivity
- **THEN** the write reports the decision as not applicable and names the connectivity satisfaction
- **AND** the page commits without a review artifact
