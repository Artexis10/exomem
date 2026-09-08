## ADDED Requirements

### Requirement: Current-source parity observations verify accepted outcomes
The comparison SHALL identify current remote revisions for both products, verify
the imported source and locked build, and run identical declared Markdown work
in fresh disposable state. It SHALL verify immediate accepted-body reads, text
visibility, typed graph edge replacement including old-edge removal, and every
acknowledged token from concurrent writes. After an acknowledged write it SHALL
kill only its owned server process, restart against the same state, and verify
accepted content and derived recovery. Process-crash evidence SHALL NOT imply
power-loss durability. Reports SHALL retain refusals and unknown outcomes and
distinguish immediate consistency from eventual catch-up. Latency comparisons
SHALL NOT present correctness-failing operations as performance wins.

#### Scenario: Latest main differs from the released wheel
- **WHEN** either current remote main is newer than the historical measured build
- **THEN** the new comparison builds and records current source separately without
  relabeling historical wheel evidence

#### Scenario: Acknowledged content survives a process crash
- **WHEN** the owned server is killed after its public write acknowledgement
- **THEN** the restarted server must return the accepted body and eventually expose
  the expected text and graph state, with recovery observations retained

#### Scenario: Search refuses while accepted writes remain readable
- **WHEN** an immediate search refuses or omits an accepted marker
- **THEN** the immediate consistency check fails even if later retrieval succeeds

### Requirement: Mixed-load observations distinguish foreground latency from catch-up
The opt-in mixed-load diagnostic SHALL run a fixed public edit/read/text-search
sequence in fresh isolated state, with and without a declared burst of pinned
PDF/OCR fixtures. It SHALL prove complete initial text-index membership before
timing and retain each client monotonic interval, outcome, and correctness result.
Media submission, observed pending work, extraction completion, and foreground
completion SHALL have separate boundaries. Pending media is not proof of active
CPU extraction. Summaries SHALL disclose sample counts and omit p95 below 100
samples; empirical percentiles SHALL NOT be represented as population guarantees.
Timeouts, unsupported schemas, refusals, missing dependencies, or failed content
proofs SHALL remain explicit and SHALL NOT be excluded to manufacture a pass.

#### Scenario: Extraction ends before foreground work
- **WHEN** media becomes complete before all foreground operations finish
- **THEN** the report separates observations inside and outside the observed media
  pending window and does not label the entire run as simultaneous extraction

#### Scenario: Too few tail observations
- **WHEN** fewer than 100 observations exist for a reported operation cohort
- **THEN** p95 is null with the sample count and the observed maximum remains visible

### Requirement: Graph catch-up is proven against final source state
The diagnostic SHALL only report graph catch-up after foreground and media writes
have stopped, when a coherent acknowledged graph generation covers the latest
checkpoint, graph recovery receipts are empty, and a read-only graph snapshot
matches the complete current Markdown file membership and source hashes. The proof
SHALL reject a legacy or empty graph, stale source bytes, changing source membership,
and lineage changes during inspection. It SHALL retain expected workload graph
content checks and report remaining non-graph queues separately. It SHALL NOT drain
or repair the running service from a second process. Unproven or timed-out catch-up
SHALL be null with the final observed reason; it SHALL NOT imply global embedding
or other projection convergence.

#### Scenario: Graph reports availability but omits a changed source
- **WHEN** a graph is available but lacks a current file or has an old source hash
- **THEN** catch-up remains unproven even if public exact text retrieval succeeds

#### Scenario: Recovery finishes after foreground closure
- **WHEN** background recovery reaches the required final graph state
- **THEN** the report records the observed catch-up bound separately from foreground
  closure, including the polling/proof overhead and remaining non-graph queues
