## ADDED Requirements

### Requirement: Doctor Reports Recall Latency From The Ledger

`exomem doctor` SHALL include a read-only `latency` check. The check SHALL read
three sources independently: the call ledger on disk, including the newest
archive generations when the trailing window reaches past the active file; the
activation log; and the in-process ring. It SHALL report each source's coverage
and, per tool and boundary, the sample count and the percentile against its
packaged bar. For a population that breaches its bar it SHALL name the stage
spans with the most total milliseconds. It SHALL withhold the calling client
from its output, because caller identities stay internal to the watch.

The check SHALL warn when a population breaches its bar. It SHALL also warn,
with a note, when the latency contract is unavailable or any source has failed
reads, malformed rows, rows whose timestamp cannot be placed in the window or
observation failures, because incomplete coverage cannot establish a pass.
Otherwise it SHALL pass, with a note when there are no samples or a population
has too few. A row with no usable timing marks its population `unavailable` in
the details without changing the status. The check MUST NOT read
note content, query text or paths. The disk readers MUST NOT apply the startup
grace period, since the cold window after a promotion is itself worth seeing in
a diagnosis.

#### Scenario: A slow tool is diagnosed, not just measured

- **WHEN** a tool's plain recalls over the window breach their bar
- **THEN** the check warns and names the tool, the boundary, the sample count, the percentile and the bar
- **AND** it lists the dominant spans among the slow calls with their milliseconds
- **AND** its output does not name the calling client

#### Scenario: A healthy, fully covered ledger passes

- **WHEN** every population is under its bar and every source was read completely
- **THEN** the check passes and still reports the figures and the source coverage in its details

#### Scenario: Incomplete coverage is not a pass

- **WHEN** a source is absent or unreadable, or holds malformed rows
- **THEN** the check warns with a note that source coverage is incomplete or that there is nothing to measure
- **AND** the check does not crash
