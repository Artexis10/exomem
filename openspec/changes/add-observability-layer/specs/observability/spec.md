## ADDED Requirements

### Requirement: Structured Event Logging

Every log record emitted through `log_event()` SHALL carry a machine-readable
`event` name, a content-free `fields` mapping, and an optional `content`
mapping restricted to the field names an `EVENT_CATALOG` entry declares for
that event. `log_event()` MUST NOT raise; an undeclared content field is
dropped rather than propagated. Log files are JSON Lines with a UTC ISO-8601
timestamp, one file per process role (`exomem.log` server, `exomem-cli.log`
CLI, `exomem-media.log` media worker) under the resolved log directory.

#### Scenario: An undeclared content field is dropped, not raised

- **WHEN** `log_event()` is called for a cataloged event with a `content` key
  the catalog does not declare for that event
- **THEN** the record is written without that key
- **AND** no exception propagates to the caller

#### Scenario: Each process role writes its own file

- **WHEN** the server process, a CLI invocation, and the media worker child
  each configure logging
- **THEN** each writes to its own file under the resolved log directory and
  none contends for another's file handle

### Requirement: Content-Safe Hosted Redaction

In a hosted cell, a log record produced by `log_event()` for an event present
in `EVENT_CATALOG` SHALL have its `content` payload dropped while its `event`
and `fields` are preserved. A record not produced this way SHALL remain fully
blanked, exactly as before this change (fail-closed). `exc_info` SHALL be
stripped from every hosted record regardless of classification.

#### Scenario: A structured record loses only its content in a hosted cell

- **WHEN** a hosted process logs a cataloged event with both `fields` and
  `content`
- **THEN** the persisted record retains `event` and `fields`
- **AND** `content` is absent from the persisted record

#### Scenario: An unstructured record stays fully blanked

- **WHEN** a hosted process logs a plain, non-`log_event()` message
- **THEN** the persisted record is fully blanked as it is today

#### Scenario: A structured record with an exception is stripped of traceback

- **WHEN** a hosted process logs a cataloged event with `exc_info` set
- **THEN** the persisted record carries no traceback or exception text

### Requirement: Metrics Registry

The system SHALL maintain one process-wide metrics registry of counters and
fixed-bucket histograms behind a single lock, covering tool call outcomes,
tool failures by code, tool duration, mutation-busy occurrences, boundary
wait/hold time and overdue count, lease operation outcomes, coordinator
errors, idempotency replays, HTTP requests, edge ingress, stale-session
serves, and log write errors. Every registry operation MUST soft-fail: a
defect in the registry MUST NOT cause a tool call, HTTP request, or mutation
to fail. The registry SHALL be snapshotted atomically to the writer-lease
state directory on an interval and restored from that snapshot at process
start, so counts survive a restart; an interval of `0` disables the
snapshotter thread entirely.

#### Scenario: A metrics failure does not fail the calling operation

- **WHEN** an internal registry operation raises
- **THEN** the calling tool call, HTTP request, or mutation completes exactly
  as it would have without the metrics call

#### Scenario: Counters survive a restart

- **WHEN** the process restarts after a metrics snapshot has been written
- **THEN** the registry's counters and histograms resume from the persisted
  snapshot rather than resetting to zero

### Requirement: Error-Plane Capture

Every site that converts an `OpError` or exception into a response envelope
(the MCP tool wrapper, the REST facade, the hosted command routes) SHALL log
a `tool_failure` or `rest_failure` event with content-free fields (tool,
request id, code, duration, scope kind) and a truncated content message, and
SHALL increment the matching metrics counters, before returning the envelope.
The envelope itself MUST remain byte-identical to its pre-change shape.

#### Scenario: An OpError is logged and counted without changing the response

- **WHEN** a tool call raises an `OpError` that is converted to a normal
  failure envelope
- **THEN** a `tool_failure` event is logged and the matching counters are
  incremented before the envelope is returned
- **AND** the returned envelope is identical to what would have been returned
  before this change

### Requirement: Tool-Call Trace Reflects Failure Outcome

`CallTraceMiddleware` SHALL emit `tool_failure code=...` instead of
`tool_success` for an MCP call whose wrapper recorded a failure, using a
bounded, lock-protected signal channel keyed by request id rather than a
context variable, because a context variable set inside the synchronous tool
wrapper does not propagate back to the asynchronous middleware layer. The
hosted `event=hosted_call kind=...` prefix contract on the `exomem.calls`
logger MUST be preserved. The signal channel MUST be bounded and unconditionally
cleared per call, with a time-based sweep, so a missed pop cannot leak
indefinitely.

#### Scenario: A converted OpError logs as a trace failure, not a success

- **WHEN** the MCP tool wrapper converts an `OpError` into a failure envelope
  for a given request id
- **THEN** `CallTraceMiddleware` logs `tool_failure code=...` for that request
  id instead of `tool_success`

#### Scenario: A successful call is unaffected

- **WHEN** a tool call completes without the wrapper recording a failure
- **THEN** `CallTraceMiddleware` logs `tool_success` exactly as before

### Requirement: Request Correlation

An ASGI access-log middleware SHALL record UTC timestamp, method, path,
status, duration, request id (extracted from an inbound header or minted, and
returned as a response header), session id, and `cf-ray` for every HTTP
request, and the unstructured `uvicorn.access` logger SHALL be silenced in the
same change so the structured record is not duplicated by an unparseable one.
The existing `logs/{queries,writes,reads}.jsonl` writers gain additive fields
(`request_id`, `ts_utc`, `outcome`, `error_code`, `duration_ms`) without
renaming or removing any existing field, and rotate at a size cap.

#### Scenario: uvicorn.access is silenced exactly when the new middleware lands

- **WHEN** the access-log middleware is wired into the server
- **THEN** `uvicorn.access` no longer emits its own line for the same request

#### Scenario: JSONL consumers see only additive fields

- **WHEN** an existing JSONL reader (`usage.py`) parses a post-change record
- **THEN** every field it previously depended on is present and unchanged

### Requirement: Mutation Journal

The system SHALL append one record per mutation attempt to
`logs/mutations.jsonl` at the terminal, interrupted, or replayed seam,
carrying timing (`duration_ms`, `boundary_wait_ms`, `boundary_hold_ms`),
outcome and error code, lease role, fencing token, replica id, and
content-classified target identifiers with a target count.

#### Scenario: A committed mutation is journaled

- **WHEN** a mutation reaches a terminal outcome
- **THEN** exactly one journal record is appended describing that attempt's
  outcome, timing, and targets

### Requirement: Observability Surfacing

A `/metrics.json` route SHALL expose the metrics registry's current snapshot
beside `/health/ready`. `runtime_readiness` SHALL include an `observability`
block, and `coordination_status` SHALL include lease TTL remaining, renewer
liveness, last renew age, the last coordinator error with its age, and lease
operation counters, recording coordinator errors explicitly instead of only
returning `coordinator_healthy: False`.

#### Scenario: A coordinator error is visible, not just a healthy flag

- **WHEN** the lease coordinator call fails
- **THEN** `coordination_status` records the error code and its age instead of
  only flipping `coordinator_healthy` to `False`

### Requirement: Operator Tooling

`exomem trace <request_id>` SHALL join the access log, tool trace, and
mutation journal for one request id into a single readable report.
`exomem logs tail|grep` SHALL operate over the per-process JSONL log files.
The doctor check SHALL verify the log directory is writable, rotation is
live, JSONL files are parseable, the metrics snapshot is recent, and the
`service.*` rotation pile is bounded.

#### Scenario: Tracing a request id joins every correlated record

- **WHEN** an operator runs `exomem trace <request_id>` for a request that
  produced an access-log line, a tool trace, and a mutation-journal record
- **THEN** the report includes all three, correlated by that request id
