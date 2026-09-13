## ADDED Requirements

### Requirement: Shared gateway placement preserves tenant isolation and public identity

The hosted platform SHALL support one shared gateway deployment beside the isolated tenant cells, reached through the existing public MCP resource identity. Its internal cell transport MUST remain within the configured private ingress boundary and retain per-cell service authentication. Gateway deployment MUST NOT make a tenant cell, provisioning interface or administrative route publicly callable or require a public OAuth endpoint per tenant.

#### Scenario: Approved public MCP request reaches a cell

- **WHEN** the public edge routes an authorized MCP request to the shared gateway
- **THEN** the gateway reaches only the mapped cell through the fixed private ingress
- **AND** website, OAuth and browser-transfer routes retain their independent existing destinations

#### Scenario: Unrelated workload attempts private access

- **WHEN** an unrelated cluster workload or public caller attempts to bypass the gateway and private ingress controls
- **THEN** network policy and cell authentication deny command access without disclosing tenant content

#### Scenario: Gateway origin is rolled back

- **WHEN** an operator restores the previous public MCP origin
- **THEN** the public resource URL, audience and existing valid OAuth grants remain unchanged

### Requirement: An interrupted first provision stays resumable by its own operation

A provision interrupted before it registers ownership beyond its namespace SHALL remain recoverable by the same operation. The provisioner SHALL authenticate and discard its own abandoned deployment record from the recorded target, the chart identity and that record's position as the newest revision, and MUST NOT require an earlier successful deployment as evidence of ownership; absence of a predecessor is not evidence of a foreign writer. Recovery SHALL preserve the operation identity, fence, request, namespace ownership, unbound storage claim, capacity reservation and tenant invite, and SHALL restore only the pre-apply checkpoint, clearing the terminal state, finalization, error code and failure-attempt budget. It MUST refuse, without mutation and without consuming its one-shot, whenever a retained artifact cannot be proven to belong to that operation, and MUST NOT clear a checkpoint to bypass a failed rollout, rebind or delete the claim, register a volume by hand, reset an invite, create a tenant or select another image.

#### Scenario: First provision is interrupted with no successful deployment

- **WHEN** the retained deployment history for a cell holds only failed revisions of the same chart plus one abandoned newest record whose recorded target equals the target the selected verified worker computes
- **THEN** the provisioner authenticates that record as its own, discards it, and proceeds with its own exact target
- **AND** the storage claim stays unbound until the binding phase submits its consumer, which is that phase's expected observation rather than a failure

#### Scenario: Retained deployment record cannot be proven to be the operation's own

- **WHEN** more than one pending record exists, the pending record is not the newest revision, any retained revision belongs to a different chart, or the recorded target differs from the computed target
- **THEN** the record remains fenced and the operation fails closed without discarding it
- **AND** an unreadable revision leaves the outcome retryable rather than terminal

#### Scenario: Reopen is requested for an operation owning only its namespace

- **WHEN** an operator reopens a terminal provision that owns only its namespace, and every precondition proves one provision for that cell, finalized with no live claim or cell lock, matching operation and provider fences and identities, a live namespace and retained provider object carrying that operation's envelope and digests, and a present unbound claim with no provider volume
- **THEN** the operation returns to pending at its pre-apply checkpoint with its identity, request, namespace record, claim, reservation and invite intact
- **AND** a second reopen of the same operation is refused by its recorded one-shot marker

#### Scenario: Reopen precondition fails

- **WHEN** any single reopen precondition does not hold
- **THEN** the reopen refuses, changes nothing, and leaves its one-shot unconsumed
