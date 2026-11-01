## ADDED Requirements

### Requirement: The CLIP Lane Admits Only Matches Above Chance

The CLIP lane in `find` SHALL admit an image or video keyframe only when its similarity to the query clears the chance maximum of the similarities of every row of the CLIP index. The population is the whole index, before the request's filters, so a filter does not change the chance maximum. The chance maximum SHALL be computed from the population's median and median absolute deviation, as the working set computes its vector band, so no cosine value is fixed in code. The band's alpha and minimum population SHALL be ranking settings, and an alpha outside the open interval (0, 1) SHALL mean no band. When the population is too small or too uniform to calibrate, or no band is set, the lane SHALL rank as it did before this requirement. The governed projected CLIP lane is outside this requirement.

#### Scenario: A text question collects no noise-level images

- **WHEN** a query's best image similarity does not clear the chance maximum
- **THEN** the CLIP lane contributes no candidate
- **AND** the trace reports the lane as available but nonmatching

#### Scenario: A clear visual match still ranks

- **WHEN** an image's similarity clears the chance maximum
- **THEN** the CLIP lane ranks it as before

#### Scenario: An uncalibrated population keeps the old lane

- **WHEN** the CLIP index holds fewer rows than the minimum population
- **THEN** the CLIP lane ranks its top images without the band
- **AND** the trace reports the band as `uncalibrated`

#### Scenario: A setting with no chance level switches the band off

- **WHEN** the alpha setting is 0 or 1
- **THEN** the CLIP lane ranks its top images without the band and does not fail
- **AND** the trace reports the band as `off`

### Requirement: The CLIP Lane Trace Reports The Band

The lane trace SHALL report the band as `applied`, `uncalibrated`, `off` or `not_attempted`. It SHALL report the number of distinct pages that the band withheld from the results the lane would return without it.

#### Scenario: The trace counts pages, not index rows

- **WHEN** the band withholds several keyframes of one video from the lane's top results
- **THEN** the trace counts that video once

#### Scenario: An empty candidate set is not called uncalibrated

- **WHEN** the request's filters leave the CLIP lane no image to rank
- **THEN** the trace reports the band as `not_attempted`

### Requirement: Question Words Do Not Choose The Ranking Intent

Intent routing SHALL NOT infer an exact lookup from the words a query starts with. A query with a quoted phrase or a wikilink SHALL still route as exact. Temporal and relationship routing SHALL be unchanged.

#### Scenario: A natural question ranks conceptually

- **WHEN** a query starts with "what", "which", "who" or "whose" and has no quote, wikilink, temporal or relationship cue
- **THEN** it routes as conceptual with neutral lane weights

#### Scenario: Syntax still marks an exact lookup

- **WHEN** a query contains a quoted phrase or a wikilink
- **THEN** it routes as exact
