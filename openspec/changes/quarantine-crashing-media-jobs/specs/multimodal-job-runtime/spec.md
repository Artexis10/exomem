## ADDED Requirements

### Requirement: A job that keeps killing the media process is blocked
When a media process dies without its own cleanup while it runs jobs, by a signal the supervisor did not send or by an exit status outside the media process protocol, each of those jobs SHALL count one crash stop. A crash stop SHALL NOT use up an attempt. After the stop limit of crash stops, the job SHALL be blocked as `child_crashed` with the death and the release named. An explicit retry, or a supervisor start under another release, SHALL return it.

#### Scenario: A job whose worker keeps crashing is blocked after the stop limit
- **WHEN** the media process dies by a signal or a native exit status each time it runs one job
- **THEN** that job is blocked as `child_crashed` after the stop limit of deaths, three by default
- **AND** its error names the last signal or exit status and the release that ran it
- **AND** status tells the owner that a new release retries the file by itself, or that a retry tries it sooner
- **AND** the next media process runs the other queued jobs

#### Scenario: A death the supervisor asked for counts nothing
- **WHEN** the supervisor or the service manager stops a media process while it runs a job
- **THEN** the job counts no crash stop and returns to the queue as before

#### Scenario: A speech job that made progress counts nothing
- **WHEN** the media process dies while a speech job runs, after the job's progress passed its last stop
- **THEN** the job keeps that progress and counts no crash stop

#### Scenario: A crash-blocked job returns
- **WHEN** a job is blocked as `child_crashed`
- **THEN** an explicit retry returns it to the queue with no crash stops
- **AND** the first start of the media supervisor under another release returns it the same way
- **AND** a start under the same release leaves it blocked
