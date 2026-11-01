## ADDED Requirements

### Requirement: The CLIP Lane Admits Only Matches Above Chance

The CLIP lane SHALL admit an image only when its similarity to the query clears the chance maximum of the similarities of the whole admitted image population. The chance maximum SHALL be computed from the population's median and median absolute deviation, as the working set computes its vector band, so no cosine value is fixed in code. The band's alpha SHALL be a ranking setting. When the population is too small or too uniform to calibrate, the lane SHALL rank as it did before this requirement. The lane trace SHALL report how many hits the band withheld.

#### Scenario: A text question collects no noise-level images

- **WHEN** a query's best image similarity does not clear the chance maximum
- **THEN** the CLIP lane contributes no candidate
- **AND** the trace reports the lane as available but nonmatching

#### Scenario: A clear visual match still ranks

- **WHEN** an image's similarity clears the chance maximum
- **THEN** the CLIP lane ranks it as before

#### Scenario: An uncalibrated population keeps the old lane

- **WHEN** the admitted image population is smaller than the minimum population
- **THEN** the CLIP lane ranks its top images without the band

### Requirement: Question Words Do Not Choose The Ranking Intent

Intent routing SHALL NOT infer an exact lookup from the words a query starts with. A query with a quoted phrase or a wikilink SHALL still route as exact. Temporal and relationship routing SHALL be unchanged.

#### Scenario: A natural question ranks conceptually

- **WHEN** a query starts with "what", "which", "who" or "whose" and has no quote, wikilink, temporal or relationship cue
- **THEN** it routes as conceptual with neutral lane weights

#### Scenario: Syntax still marks an exact lookup

- **WHEN** a query contains a quoted phrase or a wikilink
- **THEN** it routes as exact
