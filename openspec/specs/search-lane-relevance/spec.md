# search-lane-relevance Specification

## Purpose
Keep the CLIP lane from contributing images that match a question only by chance, and keep a question's leading word from choosing its ranking intent. An image or video keyframe joins the CLIP lane only when its similarity clears a chance level measured from the whole index, and the lane trace reports what that band withheld. A search that the caller restricts to pictures with `file_types` ranks the CLIP lane without the band and with its own weight.

## Requirements

### Requirement: The CLIP Lane Admits Only Matches Above Chance

The CLIP lane in `find` SHALL admit an image or video keyframe only when its similarity to the query clears the chance maximum of every row of the CLIP index, measured before the request's filters. The chance maximum SHALL come from the population's median and median absolute deviation, so no cosine is fixed in code. Its alpha and minimum population SHALL be ranking settings. A search restricted to pictures and the governed projected CLIP lane are outside this requirement.

#### Scenario: A text question collects no noise-level images

- **WHEN** a query's best image similarity does not clear the chance maximum
- **THEN** the CLIP lane contributes no candidate
- **AND** the trace reports the lane as available but nonmatching

#### Scenario: A clear visual match still ranks

- **WHEN** an image's similarity clears the chance maximum
- **THEN** the CLIP lane ranks it as before

#### Scenario: An uncalibrated population keeps the old lane

- **WHEN** the CLIP index holds fewer rows than the minimum population, or its similarities are too uniform to calibrate
- **THEN** the CLIP lane ranks its top images without the band
- **AND** the trace reports the band as `uncalibrated`

#### Scenario: A setting with no chance level switches the band off

- **WHEN** the alpha setting lies outside the open interval (0, 1)
- **THEN** the CLIP lane ranks its top images without the band and does not fail
- **AND** the trace reports the band as `off`

### Requirement: The CLIP Lane Trace Reports The Band

The lane trace SHALL report the band as `applied`, `uncalibrated`, `off` or `not_attempted`. It SHALL report the number of distinct pages that the band removed from the rows the lane fetches before the band, which are the highest-scoring index rows among the request's allowed images.

#### Scenario: The trace counts pages, not index rows

- **WHEN** the band removes several keyframes of one video from the rows the lane fetched
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

### Requirement: A Search Restricted To Pictures Ranks By Picture Match

A search SHALL be restricted to pictures when the caller's own filters require `page.file_type` to be `image` or `video`, through `file_types` or a top-level `$eq` or `$in` filter. Such a search SHALL rank the CLIP lane without the chance band and SHALL give it the ranking setting `clip_media_restricted_weight` in place of its intent weight; the other lanes keep their weights. Every other search SHALL rank as it does without this requirement.

#### Scenario: A weak picture match is ranked among pictures

- **WHEN** a search is restricted to images and the query's best image similarity does not clear the chance maximum
- **THEN** the CLIP lane still ranks the images by similarity
- **AND** the trace reports the band as `off` and `media_restricted` as true

#### Scenario: A restriction that admits notes keeps the band

- **WHEN** the caller's filters admit images and notes
- **THEN** the CLIP lane applies the chance band as for an unfiltered search

#### Scenario: A filter that names no picture kind keeps the band

- **WHEN** the caller's filters name no file kind and every page they admit happens to be a picture
- **THEN** the CLIP lane applies the chance band as for an unfiltered search

### Requirement: Search Memory Restricts To File Kinds

`search_memory` SHALL take an optional `file_types` list of file kinds, such as `image`, `video`, `audio`, `pdf` or `note`, and SHALL return only pages of those kinds. Its description SHALL tell the agent to set it when the conversation is about a photo, screenshot or video, and to search both with and without it when the agent is unsure that a picture is meant. Exomem SHALL NOT infer that a question wants a picture from the words of the query.

#### Scenario: An agent asks for images only

- **WHEN** an agent calls `search_memory` with `file_types` set to `["image"]`
- **THEN** every hit is an image sidecar

#### Scenario: A host on an older tool surface still restricts the search

- **WHEN** a host sends `file_types` inside `options`
- **THEN** the search treats it as the `file_types` parameter

#### Scenario: Omitting the restriction changes nothing

- **WHEN** an agent calls `search_memory` without `file_types`
- **THEN** the search returns every kind, ranked as before
