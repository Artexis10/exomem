## ADDED Requirements

### Requirement: Doctor Reports Recall Latency From The Ledger

`exomem doctor` SHALL include a read-only `latency` check that reads the call
ledger on disk, including the newest archive generations when the trailing
window reaches past the active file, and reports per tool and calling client
the sample count and the p50 and p90 of `total_ms` over the window, the
applicable ceiling, and for calls over the ceiling the stage spans with the
most total milliseconds. The check SHALL warn when a p90 exceeds its ceiling,
pass otherwise, pass with a note when fewer than the minimum sample count
exist, and pass with a note rather than fail when the ledger is absent or
unparseable. The check MUST NOT read note content, query text or paths from any
source, and MUST NOT apply the startup grace period, since the cold window
after a promotion is itself worth seeing in a diagnosis.

#### Scenario: A slow client is diagnosed, not just measured

- **WHEN** a client's plain recalls over the window have a p90 above the ceiling
- **THEN** the check warns, names the tool and client with samples, p50, p90 and ceiling
- **AND** lists the dominant spans among the slow calls with their milliseconds

#### Scenario: A healthy ledger passes

- **WHEN** every tool and client is under its ceiling with enough samples
- **THEN** the check passes and still reports the figures in its details

#### Scenario: An absent ledger is not a failure

- **WHEN** no ledger file exists
- **THEN** the check passes with a note that there is nothing to measure yet
