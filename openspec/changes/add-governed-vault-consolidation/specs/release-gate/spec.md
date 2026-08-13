## ADDED Requirements

### Requirement: The consolidation seal is an owner-inclusive outer release floor

Once a destination enters durable `sealed` state, the release boundary SHALL
intercept every ordinary content response before retrieval, projection, raw-byte
serialization, enumeration, resource exposure, or error assembly and SHALL
return one stable content-free sealed outcome. The rule SHALL apply to every
ordinary principal, including the owner, and to MCP, REST, CLI, Hosted, search,
ask, read/get/raw read, browse/list, graph/context, review, history, Records,
media, export/download, resource/template, upload, mutation, and background
product branches. A normal owner session, full-release grant, exact-release
approval, empty-policy fast path, escalation token, or standing rule SHALL NOT
override the seal.

The public sealed outcome SHALL reveal no run id, phase, conflict, path, title,
reference, snippet, count, score, source/destination identity, policy state,
principal policy, item existence, recovery classification, or timing-dependent
branch detail. Errors and successes SHALL cross the same terminal scrubber and
shall be normalized so a hidden item, absent item, policy transition, and
recovery branch do not become distinguishable through content. The seal SHALL
remain active throughout restrictive-policy activation, every content batch,
derived rebuild, verification, abort, rollback, and crash/retry recovery, and
SHALL be removed for ordinary routing only after the expected census, mandatory
in-process and exact-cell transport probes, routing proof, and terminal evidence
have all reached their durable verified state. The sole earlier suspension is
the exact operation's phase-bound `transport-verifying` window under trusted
control while public ingress/routing is durably stopped/drained; it grants no
ordinary public admission and must re-seal on failure/restart.

#### Scenario: Owner uses an ordinary read while content is publishing

- **WHEN** the authenticated destination owner invokes a normal ask, raw read, browse, graph, media, review, history, Record query, export, or resource route while the destination is sealed
- **THEN** it returns the same stable content-free sealed outcome as any other ordinary principal
- **AND** neither pre-cutover nor partially published content, paths, counts, policy facts, or phase facts cross the boundary

#### Scenario: Empty-policy or full-release path is present

- **WHEN** an ordinary request would otherwise use the empty-policy fast path, an L6 grant, an exact-release approval, or a standing scrubber rule while sealed
- **THEN** the outer seal wins before that path can emit content
- **AND** no ordinary authorization state weakens partial-state invisibility

#### Scenario: A content command raises during recovery

- **WHEN** a normal content command reaches a sealed destination whose recovery journal is missing, mixed, or malformed
- **THEN** its public error is the stable sealed outcome rather than the internal recovery error
- **AND** the caller cannot infer which recovery state or stored item caused the refusal

### Requirement: Only the trusted consolidation control plane may cross the outer seal

The only seal exception SHALL be an unforgeable in-process
`ConsolidationAuthority` created by trusted consolidation control code and bound
to destination vault, run, operation journal, exact phase, and allowed action.
It SHALL admit only owner-authorized reserved-run inspection, exact approved
publication or preimage restoration, and named verification probes. It SHALL
not be accepted from a command argument, authentication claim, persisted token,
serialized retry value, or ordinary owner session, and it SHALL not grant a
general read of the destination.

After a named probe crosses the outer seal, canonical identity resolution,
authorization-session binding, governance decision, disclosure-level
projection, terminal scrubbing, response adaptation, and evidence collection
SHALL all execute normally for the probe's freshly attested representative
principal and purpose. The capability SHALL NOT force an allow decision,
increase a disclosure level, reveal source-only provenance, or bypass terminal
filtering. Pre-unseal probes SHALL call those adapter/serializer functions
in-process; the authority object SHALL never cross or be reconstructed from an
MCP, REST, CLI, Hosted, retry, or other black-box request. Supplemental
transport parity SHALL be proven on disposable/cloned cells after an equivalent
seal/unseal lifecycle with normal surface authentication and no internal
authority. Real cutover SHALL also satisfy the exact-destination gate below;
clone evidence never substitutes for it. Owner-only consolidation status MAY
return bounded reserved run details through this control plane; it SHALL not
turn those details into ordinary recallable knowledge.

#### Scenario: Named negative probe runs while sealed

- **WHEN** the trusted coordinator invokes an approved negative probe with phase-bound internal authority
- **THEN** the probe crosses only the outer seal and receives the ordinary governance/projector/scrubber result for its resolved principal and purpose
- **AND** any leaked body, metadata, error, or timing oracle fails verification and keeps the seal active

#### Scenario: Owner session lacks internal authority

- **WHEN** a normal owner-authenticated request presents a run id, approval token, or capability-shaped request field to a content route
- **THEN** it cannot cross the outer seal
- **AND** the public response contains no indication whether the supplied run or token exists

#### Scenario: Supplemental black-box parity runs after seal and unseal

- **WHEN** a disposable or cloned cell completes the sealed verification lifecycle and MCP, REST, CLI, or Hosted is exercised externally
- **THEN** the request contains only normal authenticated principal/session context and traverses the real transport
- **AND** no consolidation authority is serialized or treated as transport authentication

### Requirement: Exact-cell transport verification precedes public routing

After sealed in-process verification and before public routing admission, the
real cutover SHALL enter durable `transport-stopping -> transport-verifying ->
transport-verified -> routing-opening`. The transport basis SHALL bind exact
destination post-cutover census, release/build digest, selected Hosted surface
profile/descriptor, configuration/trust/principal-mapping fingerprints, and a
trusted proof that public ingress/routing is stopped and all prior public
transport work drained.

For a Hosted exact cell, the transport basis SHALL also bind the validated
signed `HostedProfileSelection/v1` record and its current verifier-registry
generation, including the selected v3 descriptor hash and the record's bound
owner-entitlement-verifier and exact-cell transport-supervisor readiness
digests. The release gate SHALL revalidate the selection signature, signer
status/validity/revocation, and both readiness components before transport-stop
and again before routing-open. A cached startup decision, inferred lifecycle
flag, unsigned profile name, or readiness tuple from another cell SHALL not
satisfy the exact-cell gate.

Only under trusted control-plane supervision MAY the exact operation remove or
bypass its consolidation seal sufficiently for normal adapters while routing
remains durably stopped. Real MCP, REST, Hosted, and CLI calls SHALL use normal
authentication, the selected real configuration/principal mapping, and no
serialized `ConsolidationAuthority` or special principal shortcut. Positive and
negative outcomes SHALL bind the transport basis and exact plan. The supervisor
SHALL reach normal adapters through an OS/control-plane-owned isolated test
listener or equivalent route that is absent from request/authentication data and
admits only the precommitted probe set; public ingress, arbitrary local clients,
and non-probe commands remain stopped. Clone evidence
SHALL remain rehearsal evidence and SHALL not substitute for this exact-cell
gate.

Public routing SHALL open only after the transport terminal and all bound
fingerprints revalidate. Probe failure, basis drift, receipt failure, or restart
SHALL never open traffic; it SHALL deterministically restore the same
consolidation seal or retain owner-only recovery with rollback reachable.

#### Scenario: Exact cell passes normal-auth transports

- **WHEN** routing is stopped/drained and every bound MCP, REST, Hosted, and CLI positive/negative probe passes on the exact post-cutover census
- **THEN** the transport-verified terminal may authorize routing-opening
- **AND** no clone result, internal capability, or privileged test principal substitutes for real-cell behavior

#### Scenario: Restart interrupts transport verification

- **WHEN** the process restarts after the consolidation seal was temporarily removed but before transport-verified/routing-opening terminal
- **THEN** startup keeps public routing stopped and re-establishes the exact consolidation seal or owner-only recovery
- **AND** no ordinary traffic is admitted from an incomplete transport proof

#### Scenario: Ordinary local client races the transport window

- **WHEN** a normal owner or other local client that is not the supervisor-owned precommitted probe tries to use the temporarily unsealed exact cell
- **THEN** lifecycle/routing admission refuses it before the adapter while the supervisor's request still uses normal authentication inside the adapter
- **AND** no request field, principal shortcut, or serialized consolidation authority can turn that client into a transport probe

#### Scenario: Hosted transport supervisor readiness drifts

- **WHEN** the selected v3 record's signer is revoked or its bound owner-entitlement or transport-supervisor readiness digest changes before routing-open
- **THEN** exact-cell transport verification fails closed and public routing remains stopped
- **AND** v1/v2 behavior is not widened or promoted as a fallback

### Requirement: Seal coverage is registered, closed-world, and restart-safe

Every product branch capable of returning or changing vault-derived content
SHALL be registered in one seal/release coverage inventory used by command
registry validation, Hosted admission, and tests. Adding a command, selector
action, REST/MCP resource, transfer endpoint, error adapter, or background
writer without an explicit sealed-state disposition SHALL fail startup or the
release gate; it SHALL NOT default open. The persisted seal and phase SHALL load
before any surface is advertised ready after restart.

Coverage gates SHALL test success, not-found, validation, collision, busy,
timeout, cancellation, and internal-error paths under the seal and SHALL compare
principal/item-state pairs for content, metadata, count, error-shape, length,
and bounded timing equivalence. Existing per-level projectors and the terminal
scrubber remain required beneath the seal.

#### Scenario: A new raw or resource route lacks a seal disposition

- **WHEN** registry/coverage validation discovers a content-capable route that is absent from the closed-world seal inventory
- **THEN** validation fails and the route cannot ship or default to ordinary release behavior
- **AND** adding a projector without seal admission does not satisfy the gate

#### Scenario: Server restarts with a durable seal

- **WHEN** a process starts after an interruption in policy activation, publication, verification, abort, or rollback
- **THEN** the destination is sealed before MCP, REST, CLI, Hosted, transfer, resource, or background admission becomes ready
- **AND** only trusted owner control/recovery can classify and advance the run

### Requirement: Release claims stop at Exomem-mediated surfaces

The release gate and consolidation seal SHALL state their enforcement boundary
as the registered Exomem product surfaces and internal writers. Direct
filesystem or block-device access, manual copy/paste, direct private-artifact or
object-store access, and upload to an external model outside Exomem SHALL remain
outside the enforcement claim. Documentation, verification reports, receipts,
and source-retirement clearance SHALL NOT imply that those bypasses were
intercepted or audited by the release gate.

#### Scenario: Content is copied outside an Exomem command

- **WHEN** an operator reads a vault or staging file directly and pastes or uploads it outside Exomem
- **THEN** no consolidation result represents the action as release-gated, sealed, projected, scrubbed, or verified
- **AND** operational output continues to name that boundary limitation explicitly
