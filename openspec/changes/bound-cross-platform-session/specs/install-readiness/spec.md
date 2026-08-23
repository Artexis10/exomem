## ADDED Requirements

### Requirement: The cross-platform session cap sits above measured runtime

The cross-platform matrix lane SHALL bound its pytest session below its GitHub
job deadline, so that a hang is reported by pytest with its failure and timing
evidence rather than terminated silently by the runner. That session bound SHALL
sit above the runtime a healthy shard actually takes on the slowest platform in
the matrix, and SHALL leave no more than five minutes of the job deadline
unclaimed. A lane whose session bound fires below normal runtime reports a clock
as a defect, and the repository SHALL pin the relationship rather than restate
the number.

Explanatory prose SHALL NOT appear inside a folded `run:` scalar in any
workflow, because folding joins the lines and a `#` there is part of the command
rather than a comment.

#### Scenario: A healthy shard on the slowest platform completes

- **WHEN** a shard on the slowest platform in the matrix runs to completion with no failing test
- **THEN** the session bound does not fire
- **AND** the lane reports success rather than a non-zero exit after a clean summary

#### Scenario: A session genuinely hangs

- **WHEN** a shard stops making progress between test items
- **THEN** pytest requests session termination before the job deadline
- **AND** the job deadline remains the outer bound for a hang outside the session lifecycle

#### Scenario: A workflow explains a folded command

- **WHEN** a maintainer documents why a flag in a folded `run:` scalar holds its value
- **THEN** the explanation sits outside the scalar
- **AND** the command the lane runs contains no `#`
