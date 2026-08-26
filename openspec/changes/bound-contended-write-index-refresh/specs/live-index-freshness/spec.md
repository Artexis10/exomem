## MODIFIED Requirements

### Requirement: Bounded write-time index maintenance

A canonical write whose lexical index upsert cannot complete SHALL record a durable refresh demand scoped to exactly the paths of that write, drained through the bounded deferred-upsert retry or the single-flight repair owner's targeted mode. The system SHALL demand a whole-vault index refresh from a write-time failure only when the incomplete path set cannot be named exactly, and SHALL count that escalation in stable content-free telemetry.

#### Scenario: A contended batch write does not seed a whole-vault rebuild

- **WHEN** a batch atomic write completes while the lexical publication barrier is held by an active rebuild and its foreground index upsert is refused
- **THEN** the system records a durable refresh demand naming exactly that batch's changed and deleted paths
- **AND** the bounded repair machinery drains it without a whole-vault walk or full rebuild
- **AND** retrieval readiness windows remain bounded to the build that caused the contention
