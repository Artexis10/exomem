## MODIFIED Requirements

### Requirement: Structural-Pair Exemption For Write-Time Proximity Warnings

The write-time near-duplicate and overlap draft warnings — the only duplicate
mechanism in the product — SHALL suppress a candidate when the draft's own page and
that candidate are a declared pair. A pair SHALL count as declared when the reader has
recorded a matching `competing` stance, or structurally when the two pages already
carry an authored edge between them whose relation carries `opposes` (shipped:
`contradicts`), or when both carry an authored edge whose relation carries `settles`
(shipped: `answers`, `resolves`) into the same target. Suppression SHALL apply to both the
near-duplicate band and the overlap band, because genuine rivals frequently sit above
the duplicate threshold rather than below it.

Suppression SHALL apply only when the draft has an existing page identity; a draft
with no page of its own SHALL warn exactly as it does today. Suppression MUST NOT
apply to the contradiction queue or to deep packs: an authored or stanced pair still
surfaces there for review.

#### Scenario: A stanced pair stops warning at write time

- **WHEN** a page carrying a `competing` stance with a candidate is edited
- **THEN** no near-duplicate or overlap warning is emitted for that candidate
- **AND** the pair still appears in the contradiction queue

#### Scenario: An authored contradicts edge exempts the pair

- **WHEN** a page that already authors `contradicts` against a candidate is edited
- **THEN** no near-duplicate or overlap warning is emitted for that candidate, with
  no stance recorded

#### Scenario: Two answers to one question exempt the pair

- **WHEN** the edited page and the candidate both author an `answers` edge into the
  same question page
- **THEN** no near-duplicate or overlap warning is emitted for that candidate

#### Scenario: Two resolutions of one target exempt the pair

- **WHEN** the edited page and the candidate both author a `resolves` edge into the same
  target
- **THEN** no near-duplicate or overlap warning is emitted for that candidate

#### Scenario: An undeclared pair still warns

- **WHEN** the edited page and the candidate carry no stance and no declaring edge
- **THEN** the near-duplicate and overlap warnings are emitted exactly as today
