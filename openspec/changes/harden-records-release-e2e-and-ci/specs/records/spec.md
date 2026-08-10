## ADDED Requirements

### Requirement: Installed-artifact Records release journey
The release gate SHALL build and install the Exomem wheel in a clean environment, initialize a temporary human-owned vault, discover `record_memory` through a real MCP transport, and complete one bounded chronological-log Records journey without importing product code from the source checkout. The journey SHALL preserve ordinary template/manual entry, perform a guarded append and targeted guarded update, return a structured query and ephemeral derived view, round-trip an opaque Planning evidence descriptor, survive a server restart, observe a direct canonical-file edit, report its positive audit gap without silently repairing it, and keep row-only content out of ordinary semantic recall. Detailed domain, governance-reduction, mutation-crash, ambiguity, dataset, and scale cases MAY remain focused release-blocking tests rather than duplicate black-box scenarios.

#### Scenario: Manual and guarded Records state survives restart
- **WHEN** the installed product queries a manually inserted template block, appends and updates an identified item through `record_memory`, stops, receives a direct canonical-log edit, and restarts
- **THEN** the next MCP query returns the manual entry, guarded mutation, and direct edit from the same canonical Markdown file, and inspection reports an audit gap without migration, silent repair, or a competing source of truth

#### Scenario: Installed collection retains Planning and recall boundaries
- **WHEN** the installed product inspects the collection and asks ordinary memory for content that exists only in a raw record row
- **THEN** the opaque Planning reference/query descriptor round-trips unchanged and ordinary semantic recall does not return the raw canonical log

#### Scenario: Public inspection exposes only governed opaque Planning descriptors
- **WHEN** `record_memory(action="inspect")` reads a released collection containing a bounded Planning reference/query descriptor
- **THEN** `inspect.contract.plans` strictly reconstructs and returns that descriptor through the existing governance projection, without invoking an internal-only API, resolving or target-authorizing the opaque Planning reference, or revealing a withheld link-typed query value

#### Scenario: Inspection Planning projection remains default-deny
- **WHEN** a projected inspection contains an extra, malformed, over-limit, noncanonical, or untyped nested Planning descriptor value
- **THEN** the inspection egress validator refuses the invalid payload instead of passing the nested value through

#### Scenario: Unresolved remote Records access fails closed
- **WHEN** an auth-required installed HTTP MCP surface receives a protocol-valid unauthenticated raw `POST /mcp` JSON-RPC `tools/call` request naming `record_memory`
- **THEN** authenticated ingress returns exactly 401 with a Bearer challenge naming the local protected-resource metadata URL, reveals no collection, rows, Planning references, paths, or aggregates in the raw response, and leaves the separate no-auth local HTTP harness in explicit owner mode
