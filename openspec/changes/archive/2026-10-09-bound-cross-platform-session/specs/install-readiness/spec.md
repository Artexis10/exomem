## ADDED Requirements

### Requirement: The cross-platform lane carries headroom on the platform it runs on

The cross-platform matrix lane SHALL bound its pytest session below its GitHub
job deadline, so that a hang is reported by pytest with its failure and timing
evidence rather than terminated silently by the runner. That bound SHALL carry
the same headroom over predicted runtime that the pull-request tiers carry,
measured against the platform the lane actually runs on rather than the platform
the recorded durations came from: the durations file is recorded on Linux, and
this lane runs where Linux does not, so the prediction SHALL be corrected by a
measured platform factor before the headroom rule is applied. Where a split
count cannot hold that headroom inside the job deadline, the lane SHALL be split
further rather than given a longer timeout.

Explanatory prose SHALL NOT appear inside a folded `run:` scalar in any
workflow, because folding joins the lines and a `#` there is part of the command
rather than a comment.

#### Scenario: A healthy shard on the slowest platform completes

- **WHEN** a shard on the slowest platform in the matrix runs to completion with no failing test
- **THEN** the session bound does not fire
- **AND** the lane reports success rather than a non-zero exit after a clean summary

#### Scenario: The split count cannot hold the headroom

- **WHEN** the corrected prediction for the busiest shard, plus the required headroom, exceeds the job deadline
- **THEN** the repository reports that the lane needs more shards
- **AND** it does not satisfy the rule by extending the session bound past the job deadline

#### Scenario: A session genuinely hangs

- **WHEN** a shard stops making progress between test items
- **THEN** pytest requests session termination before the job deadline
- **AND** the job deadline remains the outer bound for a hang outside the session lifecycle

#### Scenario: A workflow explains a folded command

- **WHEN** a maintainer documents why a flag in a folded `run:` scalar holds its value
- **THEN** the explanation sits outside the scalar
- **AND** the command the lane runs contains no `#`
