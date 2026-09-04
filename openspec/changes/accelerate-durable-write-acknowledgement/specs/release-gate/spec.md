## ADDED Requirements

### Requirement: Exact Advisory Results Reauthorize Every Candidate At Read Time

The opaque advisory-result reference MAY address machine-local operational
state, but it MUST NOT authorize disclosure. On every exact lookup the release
plane SHALL revalidate the target generation, each counterpart generation, the
current principal, purpose, authorization session, grants, and release
projection before returning any warning or review reference. A withheld,
deleted, stale, or no-longer-authorized candidate SHALL be observationally
equivalent to absence and SHALL NOT affect a returned count, code, timing class,
or diagnostic. A materially changed target or result SHALL return
`superseded` rather than projecting the old warning.

#### Scenario: Candidate becomes withheld after computation

- **WHEN** a ready result contains a candidate that current lookup authority cannot receive
- **THEN** the candidate and every derivative are absent from the returned result
- **AND** the caller cannot distinguish withholding from a job that found no such candidate

#### Scenario: Written generation changes before lookup

- **WHEN** the result's target fingerprint no longer matches current canonical state
- **THEN** lookup returns `status="superseded"`
- **AND** no stale warning content or counterpart reference is released
