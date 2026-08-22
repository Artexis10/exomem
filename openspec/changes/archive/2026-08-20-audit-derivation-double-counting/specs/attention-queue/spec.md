## ADDED Requirements

### Requirement: Derivation double-counting is an optional deterministic review category

The `derivation_double_counting` audit category SHALL walk `sources:`
(`derived_from`) chains and surface two finding kinds. A `support_collapse`
finding SHALL be emitted for an active, writable compiled page of the types
whose frontmatter specification marks provenance required — `research-note`,
`insight`, `failure`, and `pattern` — when two or more of its directly cited
`sources:` have ancestor chains that converge on a shared node; the finding
SHALL name the nearest such shared ancestor and every contributing direct
source. The citing page itself SHALL NEVER be reported as its own shared
ancestor. One converging tail of shared ancestors SHALL produce one finding
naming the nearest node in that tail, not one finding per node in the tail.
A `cycle` finding SHALL be emitted whenever a `sources:` chain is reachable
from itself, including a direct self-reference, regardless of the
originating page's type or status. The category SHALL be optional and absent
from the default audit sweep (`ALL_CATEGORIES`) and from the `attention`
composed queue's category set; it is requested explicitly via
`audit(categories=["derivation_double_counting"])`.

Every finding SHALL be `info` or `warn` severity, never `error`: a `cycle`
finding SHALL be `warn`, a `support_collapse` finding SHALL be `info`. The
category SHALL be strictly read-only — it SHALL NOT mutate a note, rewrite a
relation, or change `find` or `attention` ranking — and SHALL propose review
rather than mutation.

#### Scenario: A diamond of derivation is surfaced as support collapse

- **WHEN** a source `S` is cited by two derived notes `A` and `B`, and a
  third active compiled note `C` cites both `A` and `B` in its `sources:`
- **AND** `derivation_double_counting` is requested explicitly
- **THEN** one `support_collapse` finding is emitted for `C`, naming `S` as
  the shared ancestor and `A`/`B` as the contributing sources

#### Scenario: Independent sources are not flagged

- **WHEN** a compiled note cites two sources whose ancestor chains never
  converge
- **THEN** no `support_collapse` finding is emitted for that note

#### Scenario: A citing page is never its own shared ancestor

- **WHEN** a compiled page cites two sources that each, transitively, cite
  the compiled page back
- **THEN** no `support_collapse` finding for that page names the page itself
  as the `shared_ancestor`

#### Scenario: One converging tail produces one finding, not one per node

- **WHEN** two of a page's directly cited sources both trace up through a
  multi-hop shared tail (for example `C` cited by both, `C` itself citing
  `D`, `D` citing `E`)
- **THEN** exactly one `support_collapse` finding is emitted for that page,
  naming the nearest node in the tail as the shared ancestor

#### Scenario: A circular `sources:` chain is detected and terminates

- **WHEN** a `sources:` chain loops back on itself, including a direct
  self-reference
- **THEN** exactly one `cycle` finding is emitted for that cycle regardless
  of how many nodes in it have outgoing `sources:` edges
- **AND** the walk terminates rather than looping indefinitely

#### Scenario: A capped traversal is visible, never silent

- **WHEN** the chain walk's depth or shared total-edge budget stops
  exploration before it completes
- **THEN** a dedicated `truncated` finding is emitted naming which cap(s)
  were actually hit (`depth`, `edges`, or both) and both configured limits
- **AND** a run that never hits either cap emits no `truncated` finding
- **AND** a per-page `support_collapse` finding computed from a truncated
  closure carries the same reason(s) in its own meta, never a generic flag
  that cannot distinguish which cap was responsible

#### Scenario: Category is opt-in and never auto-repaired

- **WHEN** an audit is computed with no `categories` filter
- **THEN** `derivation_double_counting` findings are absent from the default
  sweep
- **AND** no audit repair pass writes, infers, or removes a `sources:` entry
  on its behalf
