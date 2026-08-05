# get-payload-shape

## ADDED Requirements

### Requirement: Direct reads honor the excluded access tier

`get`/`read_memory` (and the Tier-2 direct-content reads `query_dataset` and
`read_media`) SHALL consult the `_access.yaml` access tier for the requested
path and SHALL refuse any path resolving to the `excluded` tier. The refusal
SHALL be indistinguishable from a genuinely missing path: identical error code,
identical response shape, identical message text, and no echo of the requested
path. No frontmatter, body, dataset row, media frame, or metadata of an excluded
page SHALL cross the boundary through these surfaces.

#### Scenario: Excluded page reads as missing

- **WHEN** `get`/`read_memory` is called with a path under an `excluded` subtree
- **THEN** the response is byte-identical in code, shape, and text to the
  response for a nonexistent path, and does not contain the requested path

#### Scenario: Excluded dataset and media are refused

- **WHEN** `query_dataset` targets an excluded data file, or `read_media` targets
  an excluded media artifact
- **THEN** the operation refuses with the missing-path contract and returns no
  rows and no frames

#### Scenario: Indexable content is unaffected

- **WHEN** a `read-write`, `readonly`, or `append-only` path is read
- **THEN** the response is unchanged from current behavior
