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
