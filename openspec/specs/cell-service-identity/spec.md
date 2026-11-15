# cell-service-identity Specification

## Purpose
Let a service verify a cell's identity, software, operations and limits before it sends credentials or content.

## Requirements

### Requirement: Content-free cell discovery

A Cloud cell SHALL expose `GET /.well-known/exomem-cell` without requiring a credential or tenant content.
The response SHALL name the configured cell, composed software identity, implemented operations and their effective limits.
Discovery MUST NOT read canonical content, load a model, inspect captured identities or change a registration.
It MUST NOT expose credentials, paths, hostnames, principal identifiers or captured metadata.

#### Scenario: Verify a destination before sending a credential

- **WHEN** a client requests discovery without authentication
- **THEN** the cell returns its configured identity and implemented service contract
- **AND** the client can compare that identity before sending a credential or content

#### Scenario: An operation is not implemented

- **WHEN** the composed cell lacks capture settlement or erasure
- **THEN** discovery omits that operation
- **AND** it does not advertise a timeout as a capture commit deadline

### Requirement: Report the observed software and recall identities honestly

Discovery SHALL identify the package version and deterministic package-file digest observed during server composition.
The response SHALL state that observation's scope and MUST NOT claim to attest model weights or mutable configuration.
The recall space SHALL come from the currently published embedding identity without model loading.
Its public identity SHALL be an opaque digest and SHALL equal the `recall_space_id` returned by passage search.
Unavailable software or recall identity MUST remain explicitly unknown or unavailable.

#### Scenario: A source overlay has the same package version

- **WHEN** the package files differ while their version string remains the same
- **THEN** their composed package-file digests differ
- **AND** discovery does not equate the two implementations from their version alone

#### Scenario: No recall space has been published

- **WHEN** discovery cannot obtain a published recall-space identity
- **THEN** it returns an explicit unavailable recall state
- **AND** it does not initialize an encoder to fill that field

### Requirement: Preserve authenticated content boundaries

Discovery SHALL register only on the Cloud surface and SHALL use the existing configured cell identity.
Authenticated commands SHALL retain the existing cell credential and trusted-routing checks before content handling.
Discovery MUST NOT create a credential-free route to an inventory or mutation leaf.

#### Scenario: A credential belongs to a different cell

- **WHEN** that credential reaches a content operation on this cell
- **THEN** the existing authentication boundary refuses it before the command leaf handles content
