## MODIFIED Requirements

### Requirement: The CLIP Lane Admits Only Matches Above Chance

The CLIP lane in `find` SHALL admit an image or video keyframe only when its similarity to the query clears the chance maximum of the similarities of every row of the CLIP index. The population is the whole index, before the request's filters, so a filter does not change the chance maximum. The chance maximum SHALL be computed from the population's median and median absolute deviation, as the working set computes its vector band, so no cosine value is fixed in code. The band's alpha and minimum population SHALL be ranking settings, and an alpha outside the open interval (0, 1) SHALL mean no band. When the population is too small or too uniform to calibrate, or no band is set, the lane SHALL rank as it did before this requirement. A search restricted to pictures is outside the band. The governed projected CLIP lane is outside this requirement.

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

## ADDED Requirements

### Requirement: A Search Restricted To Pictures Ranks By Picture Match

A search SHALL be restricted to pictures when the caller's own filters are active and every page they admit is the sidecar of an image or a video, the kinds the CLIP index embeds. In such a search the CLIP lane SHALL rank without the chance band, and fusion SHALL give the CLIP lane the ranking setting `clip_media_restricted_weight` in place of its intent weight. The other lanes SHALL keep their weights. A search whose filters admit any other page, and a search without filters, SHALL rank exactly as it does without this requirement. The CLIP lane trace SHALL report `media_restricted`.

#### Scenario: A weak picture match is ranked among pictures

- **WHEN** a search is restricted to images and the query's best image similarity does not clear the chance maximum
- **THEN** the CLIP lane still ranks the images by similarity
- **AND** the trace reports the band as `off` and `media_restricted` as true

#### Scenario: A restriction that admits notes keeps the band

- **WHEN** the caller's filters admit images and notes
- **THEN** the CLIP lane applies the chance band as for an unfiltered search

### Requirement: Search Memory Restricts To File Kinds

`search_memory` SHALL take an optional `file_types` list of file kinds, such as `image`, `video`, `audio`, `pdf` or `note`, and SHALL return only pages of those kinds. Its description SHALL tell the agent to set it when the conversation is about a photo, screenshot or video, and to search both with and without it when the agent is unsure that a picture is meant. Exomem SHALL NOT infer that a question wants a picture from the words of the query.

#### Scenario: An agent asks for images only

- **WHEN** an agent calls `search_memory` with `file_types` set to `["image"]`
- **THEN** every hit is an image sidecar

#### Scenario: Omitting the restriction changes nothing

- **WHEN** an agent calls `search_memory` without `file_types`
- **THEN** the search returns every kind, ranked as before
