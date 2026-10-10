## ADDED Requirements

### Requirement: The cross-platform lane carries headroom on the platform it runs on

The cross-platform matrix lane SHALL bound its pytest session below its GitHub
job deadline, so that a hang is reported by pytest with its failure and timing
evidence rather than terminated silently by the runner. That bound SHALL clear
the worst session measured on this lane by at least 15%, so that runner
variance does not turn a healthy shard into a failure. The bound SHALL NOT be
derived from `.test_durations.json`: that file records Linux times, this lane
runs on Windows and macOS, and the ratio between the two is not constant across
split counts, so no correction factor over the file predicts this lane.

Explanatory prose SHALL NOT appear inside a folded `run:` scalar in any
workflow, because folding joins the lines and a `#` there is part of the command
rather than a comment.

#### Scenario: A healthy shard on the slowest platform completes

- **WHEN** a shard on the slowest platform in the matrix runs to completion with no failing test
- **THEN** the session bound does not fire
- **AND** the lane reports success rather than a non-zero exit after a clean summary

#### Scenario: The bound loses its margin over the measured runtime

- **WHEN** the session bound is less than 15% above the worst cross-platform session measured
- **THEN** the repository's CI reliability contract fails
- **AND** a bound raised to or past the job deadline fails the same contract

#### Scenario: The Linux durations file is refreshed

- **WHEN** `.test_durations.json` is regenerated
- **THEN** the check that sizes the cross-platform session bound reads nothing from that file
- **AND** its verdict does not change

#### Scenario: A session genuinely hangs

- **WHEN** a shard stops making progress between test items
- **THEN** pytest requests session termination before the job deadline
- **AND** the job deadline remains the outer bound for a hang outside the session lifecycle

#### Scenario: A workflow explains a folded command

- **WHEN** a maintainer documents why a flag in a folded `run:` scalar holds its value
- **THEN** the explanation sits outside the scalar
- **AND** the command the lane runs contains no `#`
