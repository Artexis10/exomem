## ADDED Requirements

### Requirement: A job crash counts against each job that the media process ran
A job crash SHALL be a death of the media process while it still holds its runtime record, by a signal other than SIGTERM, SIGINT and SIGHUP, or by an exit status other than 0 and 75 to 81. A death that the supervisor caused by its own stop SHALL NOT be a job crash, nor a death already counted as a memory stop. Each job that the process ran SHALL count one job crash and SHALL keep its attempt. A preview job SHALL count none and SHALL keep its own attempt ceiling.

#### Scenario: A native fault or a native exit
- **WHEN** the media process dies by SIGSEGV, or exits with status 3, while it runs a job
- **THEN** the job counts one job crash and returns to the queue with its attempt refunded

#### Scenario: A kernel out-of-memory kill
- **WHEN** the kernel ends the media process with SIGKILL while it runs a job
- **THEN** the job counts one job crash, and a file that the kernel kills each time is blocked as `child_crashed`

#### Scenario: A process told to stop
- **WHEN** the media process dies by SIGTERM, SIGINT or SIGHUP, or the supervisor stops it
- **THEN** the job counts no job crash and returns to the queue as before

#### Scenario: A death that also ends the supervisor
- **WHEN** the media process dies with the service that supervises it
- **THEN** no job counts a job crash, and the next media process returns the job as before, with no bound on such restarts

#### Scenario: A speech job that made progress
- **WHEN** the media process dies while a speech job runs, after the job's progress passed its last stop
- **THEN** the job keeps that progress and counts no job crash

### Requirement: A job that keeps crashing the media process is blocked
Job crashes SHALL add up over the job's life under one release, not only in a row; one under another release SHALL start the count again. At the stop limit, the job SHALL be blocked as `child_crashed`, naming the death and the release. A re-requested extraction SHALL reset the count. A retry SHALL reset it and the memory, watchdog and return counts. A supervisor start under another release than the one that blocked the job SHALL return it with one try left, changing only its crash count.

#### Scenario: A job whose worker keeps crashing is blocked after the stop limit
- **WHEN** the media process dies by a job crash each time it runs one job
- **THEN** that job is blocked as `child_crashed` after the stop limit of job crashes, three by default
- **AND** its error names the last signal or exit status and the release that ran it
- **AND** status tells the owner that a new release retries the file by itself, or that a retry tries it sooner
- **AND** the next media process runs the other queued jobs

#### Scenario: A crash-blocked reprocess names a new request
- **WHEN** a reprocess of a completed transcript is blocked as `child_crashed`
- **THEN** status tells the owner that a new release retries the file by itself, or that a new reprocess request tries it sooner
- **AND** status does not offer a retry, which leaves a reprocess alone

#### Scenario: A retry returns a crash-blocked job
- **WHEN** an owner retries a job that is blocked as `child_crashed`
- **THEN** the job returns to the queue with no job crashes counted
- **AND** it is blocked again only after the stop limit of new job crashes

#### Scenario: Another release returns a crash-blocked job once
- **WHEN** the media supervisor first starts under another release than the one that blocked the job
- **THEN** the job returns to the queue, and one job crash under that release blocks it again
- **AND** a start under the same release leaves it blocked

#### Scenario: Crashes under an earlier release do not block the job
- **WHEN** a pending job counted job crashes under one release and the service now runs another
- **THEN** its next job crash counts as the first under the new release
