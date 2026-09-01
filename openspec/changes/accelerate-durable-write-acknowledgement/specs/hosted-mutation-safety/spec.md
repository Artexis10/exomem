## ADDED Requirements

### Requirement: Derived Handoff Does Not Extend Canonical Authority

For an acknowledgement-optimized governed write, mutation authority SHALL cover
preparation of exact derived-work custody and the complete canonical transaction,
but SHALL NOT cover background vector, graph, claim, lexical rebuild, resolver
rebuild, or advisory computation. A background component MUST NOT wait for its
own worker, model, or rebuild flight while holding canonical authority. Any
reader-visible derived publication SHALL continue to use that component's
existing guarded publication protocol.

#### Scenario: Slow model work follows a committed write

- **WHEN** embedding or advisory model execution takes several seconds after a governed write
- **THEN** it runs without retaining the write's canonical mutation authority
- **AND** another canonical writer can acquire authority subject to the normal writer and receipt rules

#### Scenario: Derived publication needs canonical revalidation

- **WHEN** a background component is ready to publish rows for a prepared receipt
- **THEN** it revalidates the receipt's exact current generation through its existing guarded publication seam
- **AND** it never publishes stale rows merely because the original writer once held authority

### Requirement: Derived Operational State Is Cell-Local And Governed On Retrieval

Prepared batches, pending-recall rows, advisory jobs, and advisory results SHALL
reside under the vault cell's resolved machine-local state root and SHALL NOT be
written into the synced vault. Operational status exposed by coordination,
doctor, or telemetry SHALL be content-free. Any advisory candidate content or
reference returned from exact result lookup SHALL pass current authorization,
release-plane, and secret-scrubbing policy at read time rather than inheriting
the writer's earlier disclosure decision.

#### Scenario: Hosted worker and caller are different processes

- **WHEN** a hosted mutation is acknowledged by one process and drained by another
- **THEN** both resolve the same vault-scoped state root and exact receipt namespace
- **AND** no job or result artifact appears under the governed Markdown tree

#### Scenario: Result is fetched under weaker current authority

- **WHEN** exact advisory-result lookup is performed by an audience that cannot currently receive one candidate
- **THEN** that candidate is absent from the projected result
- **AND** status, counts, codes, or timing do not reveal that it was withheld
