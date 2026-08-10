## ADDED Requirements

### Requirement: Lean suite is time-bounded and diagnosable
Each required lean Python matrix lane SHALL request pytest session termination between test items at 1,500 seconds through `--session-timeout=1500`, retain the repository's sixty-second per-item timeout, and run inside a GitHub job deadline no greater than thirty minutes. The lane SHALL report its slowest tests and write JUnit timing evidence whose path and immutable artifact name both include the matrix Python version. CI SHALL attempt to upload that evidence with `if: always()` and `if-no-files-found: warn` after ordinary success or test failure. The GitHub job deadline is the hard process bound for collection, a final in-flight item, teardown, plugin, or runner hangs and does not require an artifact from a forcibly terminated job.

#### Scenario: Contended runner reaches the requested session stop
- **WHEN** a contended runner reaches 1,500 seconds between test items
- **THEN** pytest requests session termination, reports the available failure and duration evidence, and leaves the existing per-item timeout plus outer job ceiling to bound an item already in flight

#### Scenario: Test process hangs outside its session lifecycle
- **WHEN** collection, teardown, a plugin, or the runner prevents the pytest session deadline from completing cleanly
- **THEN** the GitHub job terminates no later than thirty minutes rather than consuming the platform default timeout

#### Scenario: Ordinary lane completion preserves timing evidence
- **WHEN** a matrix lane passes or fails normally
- **THEN** its log identifies the slowest bounded set of tests and its matrix-version-specific JUnit XML is uploaded under a matrix-version-specific artifact name for comparison

### Requirement: Release-critical concurrency tests assert semantics
Release-critical mutation, serialization, critical-section, and cleanup regression tests SHALL synchronize on explicit attempts, admissions, releases, states, or injected test budgets. They SHALL NOT treat completion within a quiet-runner wall-clock threshold as the product invariant. Any remaining timeout in such a semantic test SHALL be a generous deadlock/cleanup guard, while dedicated performance or budget tests MAY retain calibrated timing assertions with an appropriate control.

#### Scenario: Same-vault contention returns retryable backpressure
- **WHEN** a test deliberately holds the same-vault mutation boundary while other writes attempt entry
- **THEN** it asserts that refused attempts are non-committed and safely retry to complete canonical state after release rather than requiring every write to fit the production acquisition timeout

#### Scenario: Boundary placement is observed structurally
- **WHEN** narrow and wide mutation modes evaluate the same validator
- **THEN** the test distinguishes them by the observed mutation-boundary state at evaluation rather than elapsed milliseconds

#### Scenario: Cleanup semantics are separated from production budget
- **WHEN** a test proves that an expired checkpoint is tombstoned and pruned
- **THEN** it uses a test-only budget sufficient for semantic completion while separate tests retain responsibility for the production prune budget
