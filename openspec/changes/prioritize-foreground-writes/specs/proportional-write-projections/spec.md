## ADDED Requirements

### Requirement: In-process bulk passes run behind writes, boundary holders and reads

Every command that the shared dispatcher runs, read or write, SHALL run as a
foreground request for in-process bulk passes, from one dispatcher mark. Every wait
for and every hold of a mutation boundary SHALL run as a foreground request, whatever
work holds the boundary. A bulk pass SHALL pause at its next unit while a foreground
request runs, within its existing progress floor. A thread that holds a mutation
boundary SHALL NOT pause. No foreground thread SHALL wait on a paused bulk pass. A
foreground thread that waits on a single-flight build or a lock that a bulk pass may
hold SHALL let every bulk pass run until that wait ends. A foreground thread that joins
a registered rebuild, which it can only do outside a boundary, SHALL leave the
foreground count for the join instead, so other requests keep their priority. A bulk
pass SHALL NOT pause while it holds any other lock that a foreground thread can wait
on. The priority SHALL be scheduling only and SHALL change no admission, proof or
publication.

#### Scenario: A write runs beside a whole-vault pass

- **WHEN** a governed write or a read runs while a bulk pass runs in the same process
- **THEN** the pass pauses at its next unit and the request's GIL-releasing phases keep about their quiet duration

#### Scenario: A drain holds the boundary during a pass

- **WHEN** the graph drain or a publication holds the mutation boundary, or a writer waits for it, while a bulk pass runs
- **THEN** the pass pauses until the hold or the wait ends, and the holder itself never pauses

#### Scenario: A request joins a build a paused pass leads

- **WHEN** a foreground request joins a single-flight build that a bulk pass leads, or waits on a lock that a bulk pass holds across its walk
- **THEN** every bulk pass runs until the wait ends, and the request waits for the work rather than for the pass's progress floor

#### Scenario: A reconcile joins a registered rebuild

- **WHEN** a foreground command joins a registered whole-vault rebuild while other foreground requests run
- **THEN** the rebuild still pauses for those requests, they keep their quiet speed, and the join completes once they end

#### Scenario: Foreground marks nest

- **WHEN** a write holds the boundary inside its dispatched command, or releases boundaries in another order than it took them
- **THEN** the thread counts once as a foreground request and the count returns to zero when its last mark ends
