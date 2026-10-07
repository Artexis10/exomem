## ADDED Requirements

### Requirement: Connector ceilings are an independent content admission floor

An armed destination SHALL meet its configured connector ceiling with existing governance decisions before empty-policy or owner shortcuts. The host SHALL define denied canonical Scope IDs and a restrictive default. Owner identity, purpose, grants, bridges, and exact releases SHALL NOT widen that ceiling. RAW authority SHALL remain a separate requirement.

#### Scenario: Empty policy meets a denied Scope

- **WHEN** an owner connector requests content in its denied Scope while ordinary policy is empty
- **THEN** the ceiling excludes that content before decode or response construction
- **AND** ordinary owner and empty-policy shortcuts cannot release it

#### Scenario: RAW authority is valid but the connector is restricted

- **WHEN** a request has current RAW authority but its connector ceiling excludes the target
- **THEN** the target remains unavailable
- **AND** the hosted RAW exemption does not exempt the connector ceiling

### Requirement: Scope membership is consistent across canonical references and paths

Markdown and canonical rows SHALL use the same Scope membership semantics for valid canonical references. Existing path-reference inclusion and exclusion SHALL remain effective. Malformed governed identity SHALL NOT create an alternate membership interpretation.

#### Scenario: Canonical-reference inclusion protects a Markdown page

- **WHEN** a protective Scope includes the page's valid canonical reference
- **THEN** Markdown and canonical-row admission both recognize its membership
- **AND** a denied connector cannot escape through a path-based read

#### Scenario: Canonical-reference exclusion and path rules remain effective

- **WHEN** a Scope excludes a valid canonical reference or uses a path rule
- **THEN** the shared membership result honors that rule on both representations
- **AND** malformed identity is handled under the existing identity validation contract

### Requirement: Hidden contributors cannot influence returned observations

Content producers SHALL admit contributors before decoding, candidate cuts, ranking, graph traversal, aggregation, or other observable derivation. Hidden bodies, paths, references, scores, links, counts, hashes, and existence SHALL NOT influence returned observations. A producer unable to compute an admitted view SHALL use its existing honest unavailable result.

#### Scenario: Protected canaries differ between twin vaults

- **WHEN** equivalent requests use the same connector ceiling over identical allowed content and different protected canaries
- **THEN** search, recall, graph, context, continuity, and canonical-state observations are equivalent
- **AND** useful allowed results remain available rather than becoming a blanket empty response

#### Scenario: A collection contains hidden rows

- **WHEN** a restricted connector queries or summarizes a collection with hidden rows
- **THEN** denied rows are excluded before their values are decoded or inspected
- **AND** summaries, counts, inspection seals, and errors depend only on admitted rows

#### Scenario: Hidden provenance or history references allowed content

- **WHEN** a hidden history, alias, citation, or provenance contributor references an allowed object
- **THEN** it cannot alter the restricted connector's returned structures or identifiers
- **AND** the allowed object remains useful through its admitted contributors

### Requirement: Every content exit and cache respects current admission

Commands, raw reads, media, frames, resources, artifacts, and transfers SHALL apply the current ceiling. Release and summary caches SHALL distinguish client and configuration revision. Result and capability consumption SHALL recheck current configuration and originating authentication. Cached intermediate packets SHALL NOT bypass admission before observable derivation.

#### Scenario: Configuration changes during a live session

- **WHEN** the host narrows a client's ceiling after a result or capability was cached
- **THEN** its next consumption applies the new ceiling
- **AND** neither an old cache entry nor an old capability releases newly denied content

#### Scenario: A hidden media artifact is requested directly

- **WHEN** a restricted connector requests hidden binary bytes, a frame, resource, or artifact handle
- **THEN** the corresponding exit applies the same ceiling before release
- **AND** direct addressing does not expose the artifact's existence or metadata

### Requirement: Whole-corpus surfaces cannot disclose hidden state

Bootstrap, registries, classification advisories, refusal counts, aggregates, receipts, provenance, and errors SHALL derive output from admitted contributors or return an existing unavailable result. They SHALL NOT expose hidden global keys, hashes, counts, or conflict facts. Denied-target and absent-target responses SHALL remain indistinguishable apart from caller-supplied information.

#### Scenario: Hidden content changes a global fingerprint

- **WHEN** hidden content changes a whole-corpus hash, registry entry, or count
- **THEN** a restricted connector observes neither that change nor a private-dependent derivative
- **AND** the producer uses an admitted calculation or its existing unavailable outcome

#### Scenario: A private definition would cause a collision

- **WHEN** a registry operation would validate against hidden global definitions before private domains are implemented
- **THEN** it remains unavailable to the restricted connector
- **AND** success or conflict cannot reveal the hidden definition

### Requirement: Enforcement claims name the Exomem boundary

Connector admission SHALL cover Exomem-mediated product operations. Documentation and evidence SHALL NOT claim that it intercepts direct filesystem access, manual copying, direct object-store access, or uploads outside Exomem.

#### Scenario: Content leaves through an external filesystem action

- **WHEN** an operator copies bytes without using an Exomem product operation
- **THEN** product evidence does not represent that action as admission-checked or verified
